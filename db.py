"""DB(Supabase PostgreSQL) 접근.
연결은 연결 풀로 한 번만 만들어 두고 다시 씀(매 질의마다 새로 연결하지 않음)."""

import csv
import hashlib
import hmac
import io
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

import psycopg2
import psycopg2.extras
import psycopg2.pool

from logic import ROLES, join_block_reason

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
PBKDF2_ITER = 60000


# ---------------------------------------------------------------------------
# 비밀번호 해시 (PBKDF2-SHA256, 표준 라이브러리)
# ---------------------------------------------------------------------------
def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), PBKDF2_ITER).hex()
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt}${h}"


def check_password(pw, stored):
    try:
        _, it, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(calc, h)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 연결
# ---------------------------------------------------------------------------
class DB:
    def __init__(self, url):
        self.url = url
        self.pool = psycopg2.pool.ThreadedConnectionPool(
            1, 5, url, cursor_factory=psycopg2.extras.RealDictCursor, connect_timeout=10
        )

    @contextmanager
    def tx(self):
        """트랜잭션 하나. 끊어진 연결이면 버리고 새 연결로 한 번 다시 시도함."""
        conn = self.pool.getconn()
        try:
            if conn.closed:
                raise psycopg2.InterfaceError("closed")
            with conn.cursor() as c:
                c.execute("select 1")
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            self.pool.putconn(conn, close=True)
            conn = self.pool.getconn()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            if not conn.closed:
                conn.rollback()
            raise
        finally:
            self.pool.putconn(conn, close=bool(conn.closed))

    # ------------------------------------------------------------------
    def init_schema(self, admin_password):
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        with self.tx() as cur:
            cur.execute(sql)
        # 관리자 계정을 만들거나 비밀번호를 갱신함
        if admin_password:
            with self.tx() as cur:
                cur.execute(
                    """insert into tf_students (student_no, name, main_role, password_hash, is_admin)
                       values ('admin', '관리자', null, %s, true)
                       on conflict (student_no) do update
                       set password_hash = excluded.password_hash, is_admin = true""",
                    (hash_password(admin_password),),
                )

    def login(self, student_no, password):
        with self.tx() as cur:
            cur.execute(
                "select id, student_no, name, is_admin, password_hash from tf_students where student_no = %s",
                (student_no.strip(),),
            )
            row = cur.fetchone()
        if row and check_password(password, row["password_hash"]):
            return {k: row[k] for k in ("id", "student_no", "name", "is_admin")}
        return None

    # ------------------------------------------------------------------
    # 화면에 그릴 상태 전체를 한 번에 읽음
    # ------------------------------------------------------------------
    def load_state(self):
        with self.tx() as cur:
            return self._load(cur)

    def _load(self, cur):
        cur.execute(
            """select s.id, s.student_no, s.name, s.main_role,
                      coalesce(array_agg(r.role order by r.role) filter (where r.role is not null), '{}') as sub_roles
               from tf_students s left join tf_student_sub_roles r on r.student_id = s.id
               where not s.is_admin
               group by s.id order by s.student_no"""
        )
        students = {r["id"]: dict(r, sub_roles=list(r["sub_roles"])) for r in cur.fetchall()}
        cur.execute("select id, name from tf_teams order by id")
        teams = []
        for r in cur.fetchall():
            teams.append({"id": r["id"], "name": r["name"], "no": team_no(r["name"]), "members": []})
        team_by_id = {t["id"]: t for t in teams}
        cur.execute("select team_id, slot_no, student_id from tf_members order by team_id, slot_no")
        member_team = {}
        for r in cur.fetchall():
            if r["team_id"] in team_by_id and r["student_id"] in students:
                team_by_id[r["team_id"]]["members"].append(students[r["student_id"]])
                member_team[r["student_id"]] = r["team_id"]
        cur.execute(
            "select id, team_id, student_id, status, reason, created_at from tf_applications "
            "where status = '대기' order by id"
        )
        pending = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "select v.application_id, v.voter_id, v.vote from tf_votes v "
            "join tf_applications a on a.id = v.application_id where a.status = '대기'"
        )
        votes = {}
        for r in cur.fetchall():
            votes.setdefault(r["application_id"], {})[r["voter_id"]] = r["vote"]
        cur.execute("select key, value from tf_settings")
        settings = {r["key"]: r["value"] for r in cur.fetchall()}
        return {
            "students": students,
            "teams": teams,
            "team_by_id": team_by_id,
            "member_team": member_team,
            "pending": pending,
            "votes": votes,
            "closed": settings.get("closed") == "true",
            "settings": settings,
        }

    def last_application(self, student_id):
        with self.tx() as cur:
            cur.execute(
                "select a.id, a.status, a.reason, t.name as team_name from tf_applications a "
                "join tf_teams t on t.id = a.team_id where a.student_id = %s order by a.id desc limit 1",
                (student_id,),
            )
            return cur.fetchone()

    # ------------------------------------------------------------------
    # 명단 올리기
    # ------------------------------------------------------------------
    def reset_with_roster(self, rows, team_count):
        with self.tx() as cur:
            cur.execute("delete from tf_votes")
            cur.execute("delete from tf_applications")
            cur.execute("delete from tf_members")
            cur.execute("delete from tf_teams")
            cur.execute("delete from tf_settings")
            cur.execute("delete from tf_student_sub_roles")
            cur.execute("delete from tf_students where not is_admin")
            for r in rows:
                pw = hash_password("1234")
                cur.execute(
                    "insert into tf_students (student_no, name, main_role, password_hash, is_admin) "
                    "values (%s, %s, %s, %s, false) returning id",
                    (r["student_no"], r["name"], r["main_role"], pw),
                )
                sid = cur.fetchone()["id"]
                for role in r["sub_roles"]:
                    cur.execute(
                        "insert into tf_student_sub_roles (student_id, role) values (%s, %s)", (sid, role)
                    )
            for i in range(1, team_count + 1):
                cur.execute("insert into tf_teams (name) values (%s)", (f"팀{i}",))
            cur.execute("insert into tf_settings (key, value) values ('closed', 'false')")

    # ------------------------------------------------------------------
    # 지원
    # ------------------------------------------------------------------
    def apply(self, student_id, team_id):
        """결과 안내 문자열을 돌려줌 ("승인: ..." / "대기: ..." / "막힘: ...")"""
        with self.tx() as cur:
            cur.execute("select id, name from tf_teams where id = %s for update", (team_id,))
            team = cur.fetchone()
            if not team:
                return "막힘: 없는 팀임"
            st = self._load(cur)
            me = st["students"].get(student_id)
            if not me:
                return "막힘: 학생 정보가 없음"
            if st["closed"]:
                return "막힘: 모집이 마감됨"
            if student_id in st["member_team"]:
                return "막힘: 이미 팀이 있음"
            if any(a["student_id"] == student_id for a in st["pending"]):
                return "막힘: 결과를 기다리는 지원이 있음"
            members = st["team_by_id"][team_id]["members"]
            reason = join_block_reason(members, me)
            if reason:
                return f"막힘: {reason}"
            if not members:
                # 빈 팀: 투표할 팀원이 없으므로 바로 승인
                cur.execute(
                    "insert into tf_applications (team_id, student_id, status, reason) "
                    "values (%s, %s, '승인', '빈 팀이라 바로 승인')",
                    (team_id, student_id),
                )
                self._add_member(cur, team_id, student_id)
                return f"승인: {team['name']}에 들어감"
            cur.execute(
                "insert into tf_applications (team_id, student_id, status) values (%s, %s, '대기')",
                (team_id, student_id),
            )
            return f"대기: {team['name']} 팀원의 투표를 기다림"

    def _add_member(self, cur, team_id, student_id):
        cur.execute("select slot_no from tf_members where team_id = %s", (team_id,))
        used = {r["slot_no"] for r in cur.fetchall()}
        free = [n for n in range(1, 5) if n not in used]
        if not free:
            raise ValueError("팀이 꽉 참")
        # (team_id, slot_no) 기본 키와 student_id 유일 제약이 동시에 들어오는 경우를 DB에서 막음
        cur.execute(
            "insert into tf_members (team_id, slot_no, student_id) values (%s, %s, %s)",
            (team_id, free[0], student_id),
        )

    # ------------------------------------------------------------------
    # 투표
    # ------------------------------------------------------------------
    def vote(self, voter_id, applicant_no, vote):
        """결과 안내: 그 지원의 지금 상태 ("승인" / "대기" / "거절" / "막힘")"""
        with self.tx() as cur:
            cur.execute(
                "select a.id, a.team_id, a.student_id, s.name from tf_applications a "
                "join tf_students s on s.id = a.student_id "
                "where s.student_no = %s and a.status = '대기' order by a.id desc limit 1",
                (applicant_no,),
            )
            app = cur.fetchone()
            if not app:
                return "막힘: 기다리는 지원이 없음"
            cur.execute("select name from tf_teams where id = %s for update", (app["team_id"],))
            team_name = cur.fetchone()["name"]
            st = self._load(cur)
            if not any(a["id"] == app["id"] for a in st["pending"]):
                return "막힘: 그 지원은 이미 처리됨"
            if st["closed"]:
                return "막힘: 모집이 마감됨"
            if st["member_team"].get(voter_id) != app["team_id"]:
                return "막힘: 그 팀의 팀원이 아님"
            if voter_id in st["votes"].get(app["id"], {}):
                return "막힘: 이미 투표함"
            cur.execute(
                "insert into tf_votes (application_id, voter_id, vote) values (%s, %s, %s)",
                (app["id"], voter_id, vote),
            )
            name = app["name"]
            if vote == "거부":
                cur.execute(
                    "update tf_applications set status = '거절', reason = '팀원이 거부함' where id = %s",
                    (app["id"],),
                )
                return f"거절: {name}의 {team_name} 지원 (팀원이 거부함)"
            return self._try_approve(cur, app, team_name)

    def _try_approve(self, cur, app, team_name):
        st = self._load(cur)
        members = st["team_by_id"][app["team_id"]]["members"]
        votes = st["votes"].get(app["id"], {})
        applicant = st["students"][app["student_id"]]
        waiting = [m["name"] for m in members if votes.get(m["id"]) != "찬성"]
        if waiting:
            return f"대기: {applicant['name']}의 {team_name} 지원 (투표 기다림: {', '.join(waiting)})"
        # 승인 직전 규칙을 다시 검사함 (같은 팀에 대기 중인 지원이 여럿일 때)
        reason = join_block_reason(members, applicant)
        if reason:
            cur.execute(
                "update tf_applications set status = '거절', reason = %s where id = %s",
                (f"승인 직전 검사: {reason}", app["id"]),
            )
            return f"거절: {applicant['name']}의 {team_name} 지원 (승인 직전 검사: {reason})"
        cur.execute(
            "update tf_applications set status = '승인', reason = '모든 팀원이 찬성' where id = %s",
            (app["id"],),
        )
        self._add_member(cur, app["team_id"], app["student_id"])
        return f"승인: {applicant['name']}이(가) {team_name}의 팀원이 됨"

    # ------------------------------------------------------------------
    # 마감
    # ------------------------------------------------------------------
    def close(self):
        with self.tx() as cur:
            cur.execute("select id from tf_teams order by id for update")
            if not cur.fetchall():
                return "막힘: 명단을 먼저 올려야 함"
            cur.execute("select value from tf_settings where key = 'closed'")
            row = cur.fetchone()
            if row and row["value"] == "true":
                return "막힘: 이미 마감됨"
            cur.execute(
                "insert into tf_settings (key, value) values ('closed', 'true') "
                "on conflict (key) do update set value = 'true'"
            )
            cur.execute(
                "update tf_applications set status = '거절', reason = '모집 마감' where status = '대기'"
            )
            rejected = cur.rowcount
            cur.execute(
                "delete from tf_members where team_id in "
                "(select team_id from tf_members group by team_id having count(*) = 1) returning student_id"
            )
            removed = cur.rowcount
            return f"승인: 모집을 마감함 (대기 지원 {rejected}건 거절, 1명뿐인 팀에서 {removed}명 빠짐)"

    # ------------------------------------------------------------------
    # 배정안 저장과 확정
    # ------------------------------------------------------------------
    def save_proposal(self, proposal):
        with self.tx() as cur:
            cur.execute(
                "insert into tf_settings (key, value) values ('proposal', %s) "
                "on conflict (key) do update set value = excluded.value",
                (json.dumps(proposal, ensure_ascii=False),),
            )

    def confirm(self, check_fn):
        """check_fn(state, proposal) -> 오류 목록. 저장한 배정안을 다시 검사한 뒤 DB에 넣음"""
        with self.tx() as cur:
            cur.execute("select id from tf_teams order by id for update")
            st = self._load(cur)
            raw = st["settings"].get("proposal")
            proposal = json.loads(raw) if raw else {}
            if "assignments" not in proposal:
                return "막힘: 확정할 배정안이 없음 (먼저 AI 배정을 누름)"
            errors = check_fn(st, proposal)
            if errors:
                return "막힘: 배정안이 지금 상태와 맞지 않음 (" + "; ".join(errors) + ")"
            by_no = {s["student_no"]: s for s in st["students"].values()}
            team_id_by_no = {t["no"]: t["id"] for t in st["teams"]}
            for item in proposal["assignments"]:
                self._add_member(cur, team_id_by_no[int(item["team"])], by_no[item["student_no"]]["id"])
            cur.execute("delete from tf_settings where key = 'proposal'")
            return f"승인: 배정을 확정함 ({len(proposal['assignments'])}명)"

    # ------------------------------------------------------------------
    # AI 호출 기록
    # ------------------------------------------------------------------
    def log_ai(self, attempt, model, outcome, detail, elapsed):
        try:
            with self.tx() as cur:
                cur.execute(
                    "insert into tf_ai_logs (attempt, model, outcome, detail, elapsed_sec) "
                    "values (%s, %s, %s, %s, %s)",
                    (attempt, model, outcome, (detail or "")[:1000], round(elapsed, 1)),
                )
        except Exception:
            pass

    def recent_ai_logs(self, limit=12):
        with self.tx() as cur:
            cur.execute(
                "select created_at, attempt, model, outcome, detail, elapsed_sec from tf_ai_logs "
                "order by id desc limit %s",
                (limit,),
            )
            return cur.fetchall()


def team_no(name):
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 0


# ---------------------------------------------------------------------------
# 명단 CSV 읽기
# ---------------------------------------------------------------------------
def parse_roster(data: bytes):
    """CSV(UTF-8, BOM 있어도 됨)를 읽어 [{student_no, name, main_role, sub_roles}] 를 돌려줌.
    틀린 곳이 있으면 ValueError"""
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    need = {"student_no", "name", "main_role", "sub_roles"}
    if not reader.fieldnames or not need.issubset({(f or "").strip() for f in reader.fieldnames}):
        raise ValueError("열 이름이 student_no,name,main_role,sub_roles 이어야 함")
    rows, seen = [], set()
    for i, raw in enumerate(reader, start=2):
        r = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        if not any(r.values()):
            continue
        no, name, main = r["student_no"], r["name"], r["main_role"]
        if not no or not name:
            raise ValueError(f"{i}번째 줄: 학번이나 이름이 비어 있음")
        if no.lower() == "admin":
            raise ValueError(f"{i}번째 줄: admin 은 학번으로 쓸 수 없음")
        if no in seen:
            raise ValueError(f"{i}번째 줄: 학번 {no} 가 두 번 나옴")
        seen.add(no)
        if main not in ROLES:
            raise ValueError(f"{i}번째 줄: 주 역할 '{main}' 은 6개 역할에 없음")
        subs = [x.strip() for x in r["sub_roles"].split(";") if x.strip()]
        for s in subs:
            if s not in ROLES:
                raise ValueError(f"{i}번째 줄: 보조 역할 '{s}' 은 6개 역할에 없음")
        if len(subs) > 2:
            raise ValueError(f"{i}번째 줄: 보조 역할은 2개까지임")
        rows.append({"student_no": no, "name": name, "main_role": main, "sub_roles": subs})
    if not rows:
        raise ValueError("학생이 한 명도 없음")
    return rows


def get_secret(name):
    """.env(로컬) 또는 Streamlit Secrets(배포)에서 값을 읽음. 값은 화면에 출력하지 않음."""
    v = os.environ.get(name)
    if v:
        return v
    try:
        import streamlit as st

        return st.secrets.get(name)
    except Exception:
        return None
