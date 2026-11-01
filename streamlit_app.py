"""팀 편성 앱 (Streamlit)

실행: streamlit run streamlit_app.py
키(OPENROUTER_API_KEY, DATABASE_URL, ADMIN_PASSWORD)는 .env 또는 Streamlit Secrets 에 둠.
"""

import json
from zoneinfo import ZoneInfo

import streamlit as st
from dotenv import load_dotenv

import ai
from db import DB, get_secret, parse_roster
from logic import SearchTimeout, diagnose_infeasible, solve_assignment, validate_assignment

load_dotenv()
st.set_page_config(page_title="팀 편성", page_icon="👥", layout="wide")

KST = ZoneInfo("Asia/Seoul")
REFRESH_SEC = 3  # 다른 사람의 변경을 자동으로 다시 그리는 간격(5초 이하)
ss = st.session_state


# ---------------------------------------------------------------------------
# DB 연결 (앱이 시작할 때 한 번만 만들고 다시 씀)
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="DB에 연결하는 중…")
def get_db():
    url = get_secret("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL 없음")
    db = DB(url)
    db.init_schema(get_secret("ADMIN_PASSWORD"))  # 테이블이 없으면 만들고, 관리자 계정을 만들거나 갱신함
    return db


try:
    db = get_db()
except Exception as e:
    st.error(f"DB에 연결하지 못했습니다. .env 또는 Secrets 의 DATABASE_URL 을 확인하세요. ({type(e).__name__})")
    st.stop()


def ai_models():
    raw = get_secret("AI_MODELS")
    if raw:
        models = [m.strip() for m in raw.split(",") if m.strip()]
        if models:
            return models[:3]
    return ai.DEFAULT_MODELS


# ---------------------------------------------------------------------------
# 공통 표시
# ---------------------------------------------------------------------------
def role_text(s):
    subs = ", ".join(s["sub_roles"])
    return f"{s['main_role']}" + (f" (보조: {subs})" if subs else "")


def show_team(team, highlight=False):
    with st.container(key=f"team_{team['no']}", border=True):
        title = f"**{team['name']}**  ·  {len(team['members'])}/4명"
        if highlight:
            title += "  ·  :blue-badge[내 팀]"
        st.markdown(title)
        if team["members"]:
            for m in team["members"]:
                st.markdown(f"- {m['name']} — {role_text(m)}")
        else:
            st.caption("팀원 없음")


def team_grid(state, my_team_id=None, apply_fn=None):
    teams = state["teams"]
    cols = st.columns(3)
    for i, t in enumerate(teams):
        with cols[i % 3]:
            show_team(t, highlight=(t["id"] == my_team_id))
            if apply_fn:
                st.button(
                    f"{t['name']}에 지원",
                    key=f"apply_{t['no']}",
                    on_click=apply_fn,
                    args=(t["id"],),
                    width="stretch",
                )


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        return f"막힘: 처리 중 오류가 남 ({type(e).__name__}: {str(e)[:120]})"


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------
def do_login():
    no = ss.get("login_student_no", "").strip()
    pw = ss.get("login_password", "")
    try:
        user = db.login(no, pw)
    except Exception as e:
        ss.result = f"막힘: 로그인 중 오류가 남 ({type(e).__name__})"
        return
    if user:
        ss.user = user
        ss.result = f"승인: {user['name']}(으)로 로그인함"
    else:
        ss.result = "막힘: 학번 또는 비밀번호가 틀림"


def do_logout():
    for k in ("user", "result", "assign_status"):
        ss.pop(k, None)


def login_page():
    st.title("👥 팀 편성")
    with st.container(border=True):
        st.text_input("학번", key="login_student_no")
        st.text_input("비밀번호", type="password", key="login_password")
        st.button("로그인", key="login_submit", on_click=do_login, type="primary")
    with st.container(key="result"):
        if ss.get("result"):
            st.write(ss.result)


def header():
    user = ss.user
    c1, c2 = st.columns([5, 1])
    with c1:
        with st.container(key="whoami"):
            if user["is_admin"]:
                st.markdown(f"### 🛠️ {user['name']} (admin)")
            else:
                st.markdown(f"### 🙋 {user['name']} ({user['student_no']})")
    with c2:
        st.button("로그아웃", key="logout", on_click=do_logout)


# ---------------------------------------------------------------------------
# 학생 화면
# ---------------------------------------------------------------------------
def on_apply(team_id):
    ss.result = safe(db.apply, ss.user["id"], team_id)


def on_vote(applicant_no, vote):
    ss.result = safe(db.vote, ss.user["id"], applicant_no, vote)


@st.fragment(run_every=REFRESH_SEC)
def student_view():
    me = ss.user
    with st.container(key="result"):
        if ss.get("result"):
            st.write(ss.result)
    try:
        state = db.load_state()
    except Exception as e:
        st.warning(f"DB를 읽지 못했습니다. 잠시 뒤 다시 그립니다. ({type(e).__name__})")
        return

    my_team_id = state["member_team"].get(me["id"])
    my_pending = [a for a in state["pending"] if a["student_id"] == me["id"]]
    team_name = {t["id"]: t["name"] for t in state["teams"]}

    if state["closed"]:
        st.info("모집이 마감되었습니다.")
    if my_team_id:
        st.success(f"내 팀: {team_name[my_team_id]}")
    else:
        st.write("아직 팀이 없음")
    if my_pending:
        st.info(f"결과를 기다리는 지원: {team_name.get(my_pending[0]['team_id'], '?')}")
    elif not my_team_id:
        last = db.last_application(me["id"])
        if last and last["status"] == "거절":
            st.caption(f"지난 지원: {last['team_name']} 거절 ({last['reason'] or ''})")

    can_apply = not state["closed"] and not my_team_id and not my_pending
    st.subheader("팀 목록")
    team_grid(state, my_team_id, on_apply if can_apply else None)

    if my_team_id:
        st.subheader("내 팀에 들어온 지원")
        apps = [a for a in state["pending"] if a["team_id"] == my_team_id]
        if not apps:
            st.caption("기다리는 지원이 없음")
        for a in apps:
            applicant = state["students"][a["student_id"]]
            votes = state["votes"].get(a["id"], {})
            with st.container(border=True):
                st.markdown(f"**{applicant['name']}** ({applicant['student_no']}) — {role_text(applicant)}")
                members = state["team_by_id"][my_team_id]["members"]
                st.caption(
                    "투표: "
                    + ", ".join(f"{m['name']} {votes.get(m['id'], '아직')}" for m in members)
                )
                mine = votes.get(me["id"])
                if mine:
                    st.write(f"내 투표: {mine}")
                elif not state["closed"]:
                    c1, c2, _ = st.columns([1, 1, 4])
                    no = applicant["student_no"]
                    c1.button("찬성", key=f"vote_yes_{no}", on_click=on_vote, args=(no, "찬성"))
                    c2.button("거부", key=f"vote_no_{no}", on_click=on_vote, args=(no, "거부"))


# ---------------------------------------------------------------------------
# 관리자 화면
# ---------------------------------------------------------------------------
def on_start():
    f = ss.get("admin_roster_file")
    count = int(ss.get("admin_team_count") or 0)
    if f is None:
        ss.result = "막힘: 명단 CSV 파일을 먼저 올려야 함"
        return
    if count < 1:
        ss.result = "막힘: 팀 수는 1 이상이어야 함"
        return
    try:
        rows = parse_roster(f.getvalue())
    except Exception as e:
        ss.result = f"막힘: 명단을 읽지 못함 ({e})"
        return
    try:
        db.reset_with_roster(rows, count)
    except Exception as e:
        ss.result = f"막힘: 처음 상태를 만들지 못함 ({type(e).__name__}: {str(e)[:120]})"
        return
    ss.pop("assign_status", None)
    ss.result = f"승인: 학생 {len(rows)}명과 팀 {count}개로 처음 상태를 만듦"


def on_close():
    ss.result = safe(db.close)


def split_state(state):
    teams = {t["no"]: list(t["members"]) for t in state["teams"]}
    unassigned = [s for sid, s in state["students"].items() if sid not in state["member_team"]]
    return teams, unassigned


def check_proposal(state, proposal):
    teams, unassigned = split_state(state)
    pairs = [(it["student_no"], int(it["team"])) for it in proposal.get("assignments", [])]
    return validate_assignment(teams, unassigned, pairs)


def on_confirm():
    ss.result = safe(db.confirm, check_proposal)
    if ss.result.startswith("승인"):
        ss.assign_status = "배정을 확정함"


def run_assign(status_ph, result_ph):
    """AI 배정: 1차 → 2차 → 3차 모델, 모두 실패하면 앱이 스스로 찾음"""

    def finish(result, status):
        ss.result = result
        ss.assign_status = status
        result_ph.write(result)
        status_ph.markdown(status)

    state = db.load_state()
    if not state["teams"]:
        return finish("막힘: 명단을 먼저 올려야 함", "")
    if not state["closed"]:
        return finish("막힘: 모집을 먼저 마감해야 함", "")
    teams, unassigned = split_state(state)

    # 1) 규칙을 지키는 배정이 있는지 먼저 확인 (없으면 AI를 부르지 않음)
    status_ph.info("규칙을 지키는 배정이 있는지 확인하는 중…")
    try:
        backup = solve_assignment(teams, unassigned, time_limit=40)
        search_done = True
    except SearchTimeout:
        backup, search_done = None, False
    if search_done and backup is None:
        reasons = diagnose_infeasible(teams, unassigned)
        db.save_proposal({})  # 지난 배정안 지움
        return finish(
            "막힘: 규칙을 지키는 배정이 없음",
            "**규칙을 지키는 배정이 없음**\n\n" + "\n".join(f"- {r}" for r in reasons),
        )
    if not unassigned:
        db.save_proposal({"assignments": [], "source": "배정할 학생 없음"})
        return finish("승인: 팀이 없는 학생이 없음. 확정을 누르면 끝남", "팀이 없는 학생이 없음")

    # 2) AI에게 차례로 요청
    key = get_secret("OPENROUTER_API_KEY")
    prompt = ai.build_prompt(teams, unassigned)
    log_lines = []
    proposal_pairs, source = None, None
    for i, model in enumerate(ai_models(), start=1):
        if not key:
            log_lines.append(f"- {i}차 모델({model}): 실패 — OPENROUTER_API_KEY 없음")
            continue

        def tick(el, i=i, model=model):
            status_ph.info(
                "\n".join(log_lines)
                + f"\n\n⏳ **{i}차 모델**({model})의 응답을 기다리는 중… {int(el)}초 / {ai.MODEL_TIMEOUT}초"
            )

        tick(0)
        text, err, elapsed = ai.call_model(key, model, prompt, on_tick=tick)
        if err is None:
            try:
                pairs = ai.parse_reply(text)
                errors = validate_assignment(teams, unassigned, pairs)
                if errors:
                    err = "검사 통과 못 함: " + "; ".join(errors[:3])
                else:
                    proposal_pairs, source = pairs, f"{i}차 모델({model})"
            except Exception as e:
                err = f"응답을 읽지 못함: {str(e)[:120]}"
        if proposal_pairs:
            log_lines.append(f"- {i}차 모델({model}): 성공 ({elapsed:.0f}초)")
            db.log_ai(i, model, "성공", "", elapsed)
            break
        log_lines.append(f"- {i}차 모델({model}): 실패 — {err} ({elapsed:.0f}초)")
        db.log_ai(i, model, "실패", err, elapsed)

    # 3) 3차까지 실패하면 앱이 스스로 찾은 배정을 씀
    if not proposal_pairs:
        status_ph.info("\n".join(log_lines) + "\n\n3차 모델까지 실패하여 앱이 스스로 배정을 찾는 중…")
        if backup is None:
            try:
                backup = solve_assignment(teams, unassigned, time_limit=90)
            except SearchTimeout:
                backup = None
        if backup is None:
            db.save_proposal({})
            return finish(
                "막힘: 배정안을 만들지 못함",
                "\n".join(log_lines) + "\n\n앱도 정해진 시간 안에 배정을 찾지 못함",
            )
        proposal_pairs, source = backup, "앱이 스스로 찾은 배정 (3차 모델까지 실패)"
        db.log_ai(0, "앱 자체 배정", "성공", "모든 경우를 따져 찾음", 0)

    proposal = {
        "assignments": [{"student_no": no, "team": t} for no, t in proposal_pairs],
        "source": source,
    }
    db.save_proposal(proposal)
    finish(
        f"승인: 배정안을 만듦 ({source}). 확인한 뒤 확정을 누름",
        "\n".join(log_lines) + f"\n\n✅ 배정안이 검사 다섯 가지를 모두 통과함 — {source}",
    )


def show_proposal(state):
    raw = state["settings"].get("proposal")
    if not raw:
        return
    p = json.loads(raw)
    if not p.get("assignments"):
        return
    by_no = {s["student_no"]: s for s in state["students"].values()}
    rows = []
    for it in sorted(p["assignments"], key=lambda x: (int(x["team"]), x["student_no"])):
        s = by_no.get(it["student_no"])
        if s:
            rows.append(
                {
                    "이름": s["name"],
                    "학번": s["student_no"],
                    "주 역할": s["main_role"],
                    "보조 역할": ", ".join(s["sub_roles"]),
                    "배정된 팀": f"팀{it['team']}",
                }
            )
    st.markdown(f"**배정안** — {p.get('source', '')}")
    st.dataframe(rows, hide_index=True, width="stretch")


def admin_page():
    with st.container(key="result"):
        result_ph = st.empty()
        if ss.get("result"):
            result_ph.write(ss.result)

    st.subheader("1. 명단으로 처음 상태 만들기")
    c1, c2 = st.columns([3, 1])
    c1.file_uploader("명단 CSV (UTF-8)", type=["csv"], key="admin_roster_file")
    c2.number_input("팀 수", min_value=1, max_value=50, value=3, step=1, key="admin_team_count")
    st.button("명단으로 처음 상태 만들기", key="admin_start", on_click=on_start)
    st.caption("누르면 지원, 투표, 팀원, 마감 표시를 모두 지우고 새로 시작합니다. 학생 비밀번호는 모두 1234입니다.")

    st.subheader("2. 모집 마감")
    st.button("모집 마감", key="admin_close", on_click=on_close)

    st.subheader("3. 남은 학생 배정")
    clicked = st.button("AI 배정", key="admin_assign", type="primary")
    with st.container(key="assign_status", border=True):
        status_ph = st.empty()
        if not clicked:
            status_ph.markdown(ss.get("assign_status") or "아직 배정하지 않음")
    if clicked:
        try:
            run_assign(status_ph, result_ph)
        except Exception as e:
            ss.result = f"막힘: 배정 중 오류가 남 ({type(e).__name__}: {str(e)[:120]})"
            result_ph.write(ss.result)
    admin_live()
    st.button("배정안 확정", key="admin_confirm", on_click=on_confirm)

    with st.expander("AI 호출 기록"):
        try:
            logs = db.recent_ai_logs()
        except Exception:
            logs = []
        if not logs:
            st.caption("기록 없음")
        for r in logs:
            t = r["created_at"].astimezone(KST).strftime("%m/%d %H:%M:%S")
            step = f"{r['attempt']}차" if r["attempt"] else "앱"
            st.markdown(f"- {t} · {step} · {r['model']} · {r['outcome']} · {r['elapsed_sec']}초 · {r['detail'] or ''}")
    st.caption(f"1차, 2차, 3차 모델: {', '.join(ai_models())} · 모델마다 최대 {ai.MODEL_TIMEOUT}초")


@st.fragment(run_every=REFRESH_SEC)
def admin_live():
    try:
        state = db.load_state()
    except Exception as e:
        st.warning(f"DB를 읽지 못했습니다. ({type(e).__name__})")
        return
    show_proposal(state)
    st.markdown("**팀이 없는 학생**")
    with st.container(key="unassigned", border=True):
        _, unassigned = split_state(state)
        if unassigned:
            for s in unassigned:
                st.markdown(f"- {s['name']} — {role_text(s)}")
        else:
            st.caption("없음")
    st.markdown(f"**팀 현황** {'· 마감됨' if state['closed'] else '· 모집 중'}  ·  대기 중인 지원 {len(state['pending'])}건")
    team_grid(state)


# ---------------------------------------------------------------------------
if "user" not in ss:
    login_page()
else:
    header()
    if ss.user["is_admin"]:
        admin_page()
    else:
        student_view()
