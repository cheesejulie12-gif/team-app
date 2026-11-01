"""규칙 검사와 배정 탐색 (DB, Streamlit과 무관한 순수 함수)

학생 하나는 dict 로 나타냄:
    {"id": 1, "student_no": "2026001", "name": "가나다",
     "main_role": "기획자", "sub_roles": ["보고서 작성자"]}
"""

import time

ROLES = ["기획자", "웹 프로그래머", "프론트엔드 디자이너", "테스터", "보고서 작성자", "발표자"]
PLANNER = "기획자"
WEB = "웹 프로그래머"
MAX_TEAM = 4
MIN_TEAM = 2


# ---------------------------------------------------------------------------
# 역할
# ---------------------------------------------------------------------------
def roles_of(s):
    return {s["main_role"], *s.get("sub_roles", [])}


def can_planner(s):
    return PLANNER in roles_of(s)


def can_web(s):
    return WEB in roles_of(s)


def required_filled(members):
    """필수 포함 역할(기획자, 웹 프로그래머) 가운데 채운 수(0~2).
    두 역할은 서로 다른 학생이 맡아야 하므로, 한 학생이 둘 다 할 수 있어도 하나로만 셈."""
    p = {s["id"] for s in members if can_planner(s)}
    w = {s["id"] for s in members if can_web(s)}
    if p and w and len(p | w) >= 2:
        return 2
    if p or w:
        return 1
    return 0


def missing_required_roles(members):
    """아직 채우지 못한 필수 포함 역할 이름 목록"""
    filled = required_filled(members)
    if filled == 2:
        return []
    p = [s for s in members if can_planner(s)]
    w = [s for s in members if can_web(s)]
    if filled == 0:
        return [PLANNER, WEB]
    # filled == 1
    if not p:
        return [PLANNER]
    if not w:
        return [WEB]
    # 같은 한 학생만 둘 다 할 수 있는 경우
    return [WEB if p[0]["main_role"] == PLANNER else PLANNER]


def has_main_dup(members, newcomer):
    return any(m["main_role"] == newcomer["main_role"] for m in members)


def seat_short(members_after):
    """지원자가 들어간 뒤 남는 자리 수가, 아직 채우지 못한 필수 포함 역할 수보다 적은가"""
    remaining = MAX_TEAM - len(members_after)
    unfilled = 2 - required_filled(members_after)
    return remaining < unfilled


def join_block_reason(members, newcomer):
    """newcomer가 이 팀에 들어갈 수 없으면 이유, 들어갈 수 있으면 None.
    (지원할 때와 승인 직전 검사에 같이 씀)"""
    if len(members) >= MAX_TEAM:
        return "팀이 꽉 참"
    if has_main_dup(members, newcomer):
        return "같은 주 역할의 팀원이 있음"
    if seat_short(members + [newcomer]):
        return "필수 포함 역할(기획자, 웹 프로그래머)을 맡을 학생의 자리가 모자람"
    return None


def team_ok(members):
    return (
        MIN_TEAM <= len(members) <= MAX_TEAM
        and len({m["main_role"] for m in members}) == len(members)
        and required_filled(members) == 2
    )


# ---------------------------------------------------------------------------
# 배정안 검사 (2.5절 4항의 다섯 가지)
# ---------------------------------------------------------------------------
def validate_assignment(teams, unassigned, proposal):
    """teams: {팀번호: [학생,...]} (지금 팀원), unassigned: [학생,...]
    proposal: [(student_no, 팀번호), ...]
    통과하면 빈 목록, 아니면 실패 이유 목록"""
    errors = []
    by_no = {s["student_no"]: s for s in unassigned}
    seen = {}
    for no, t in proposal:
        no = str(no).strip()
        if no not in by_no:
            errors.append(f"팀이 없는 학생이 아닌 학번이 들어 있음: {no}")
            continue
        if no in seen and seen[no] != t:
            errors.append(f"한 학생이 여러 팀에 배정됨: {by_no[no]['name']}")
        elif no in seen:
            errors.append(f"같은 학생이 두 번 배정됨: {by_no[no]['name']}")
        seen[no] = t
        if t not in teams:
            errors.append(f"없는 팀 번호: {t}")
    # 1. 빠짐없이
    missing = [s["name"] for s in unassigned if s["student_no"] not in seen]
    if missing:
        errors.append("배정되지 않은 학생이 있음: " + ", ".join(missing))
    if errors:
        return errors
    final = {t: list(ms) for t, ms in teams.items()}
    for no, t in seen.items():
        final[t].append(by_no[no])
    for t in sorted(final):
        ms = final[t]
        # 3. 인원
        if not (MIN_TEAM <= len(ms) <= MAX_TEAM):
            errors.append(f"팀{t}의 인원이 {len(ms)}명임 (2~4명이어야 함)")
        # 4. 주 역할 중복
        mains = [m["main_role"] for m in ms]
        dups = sorted({r for r in mains if mains.count(r) > 1})
        if dups:
            errors.append(f"팀{t}에 주 역할이 겹침: {', '.join(dups)}")
        # 5. 필수 포함 역할
        miss = missing_required_roles(ms)
        if miss:
            errors.append(f"팀{t}에 {', '.join(miss)} 역할을 맡을 학생이 없음")
    return errors


# ---------------------------------------------------------------------------
# 앱이 스스로 하는 배정: 모든 경우를 따져 끝까지 찾음 (백트래킹)
# ---------------------------------------------------------------------------
class SearchTimeout(Exception):
    pass


def solve_assignment(teams, unassigned, time_limit=60.0):
    """규칙을 모두 지키는 배정을 찾음.
    찾으면 [(student_no, 팀번호), ...], 없으면 None.
    time_limit 안에 끝내지 못하면 SearchTimeout."""
    team_ids = sorted(teams)
    cur = {t: list(teams[t]) for t in team_ids}
    deadline = time.monotonic() + time_limit
    counter = [0]

    def adds_required(t, s):
        return required_filled(cur[t] + [s]) > required_filled(cur[t])

    def fits(t, s):
        return len(cur[t]) < MAX_TEAM and not has_main_dup(cur[t], s)

    def feasible(rest):
        r = len(rest)
        need_min = 0
        seats = 0
        for t in team_ids:
            ms = cur[t]
            free = MAX_TEAM - len(ms)
            seats += free
            need_min += max(0, MIN_TEAM - len(ms))
            unfilled = 2 - required_filled(ms)
            if unfilled > free:
                return False
            if unfilled > 0 and not any(fits(t, s) and adds_required(t, s) for s in rest):
                return False
            if len(ms) < MIN_TEAM and sum(1 for s in rest if fits(t, s)) < MIN_TEAM - len(ms):
                return False
        if not (need_min <= r <= seats):
            return False
        # 전체 개수로 따지는 필요조건 (한 학생은 한 팀에만 들어가므로)
        no_p = sum(1 for t in team_ids if not any(can_planner(m) for m in cur[t]))
        no_w = sum(1 for t in team_ids if not any(can_web(m) for m in cur[t]))
        if no_p > sum(1 for s in rest if can_planner(s)):
            return False
        if no_w > sum(1 for s in rest if can_web(s)):
            return False
        unfilled_total = sum(2 - required_filled(cur[t]) for t in team_ids)
        if unfilled_total > sum(1 for s in rest if can_planner(s) or can_web(s)):
            return False
        # 주 역할마다: 그 주 역할의 학생 수 ≤ 그 주 역할이 없고 자리가 남은 팀 수
        mains = {}
        for s in rest:
            mains[s["main_role"]] = mains.get(s["main_role"], 0) + 1
        for role, k in mains.items():
            room = sum(
                1 for t in team_ids
                if len(cur[t]) < MAX_TEAM and all(m["main_role"] != role for m in cur[t])
            )
            if k > room:
                return False
        return True

    def rec(rest):
        counter[0] += 1
        if counter[0] % 200 == 0 and time.monotonic() > deadline:
            raise SearchTimeout()
        if not rest:
            return [] if all(team_ok(cur[t]) for t in team_ids) else None
        if not feasible(rest):
            return None
        # 갈 수 있는 팀이 가장 적은 학생부터 (MRV)
        best = None
        for s in rest:
            opts = [t for t in team_ids if fits(t, s)]
            if best is None or len(opts) < len(best[1]):
                best = (s, opts)
                if not opts:
                    return None
        s, opts = best
        # 필수 역할을 채워 주는 팀을 먼저 시도
        opts.sort(key=lambda t: (not adds_required(t, s), len(cur[t])))
        rest2 = [x for x in rest if x is not s]
        tried_empty = False
        for t in opts:
            if not cur[t]:
                # 빈 팀끼리는 서로 같으므로 하나만 시도
                if tried_empty:
                    continue
                tried_empty = True
            cur[t].append(s)
            res = rec(rest2)
            cur[t].pop()
            if res is not None:
                return [(s["student_no"], t)] + res
        return None

    return rec(list(unassigned))


def diagnose_infeasible(teams, unassigned):
    """규칙을 지키는 배정이 없을 때 그 이유 목록"""
    reasons = []
    free_total = sum(MAX_TEAM - len(ms) for ms in teams.values())
    need_total = sum(max(0, MIN_TEAM - len(ms)) for ms in teams.values())
    if len(unassigned) > free_total:
        reasons.append(f"팀이 없는 학생 {len(unassigned)}명보다 남은 자리({free_total}개)가 적음")
    if len(unassigned) < need_total:
        reasons.append(
            f"모든 팀을 2명 이상으로 채우려면 {need_total}명이 더 필요한데 팀이 없는 학생은 {len(unassigned)}명임"
        )
    for t in sorted(teams):
        ms = teams[t]
        free = MAX_TEAM - len(ms)
        miss = missing_required_roles(ms)
        if miss and len(ms) >= MAX_TEAM:
            reasons.append(f"팀{t}은 꽉 찼는데 {', '.join(miss)} 역할을 맡을 학생이 없음")
            continue
        if len(miss) > free:
            reasons.append(f"팀{t}에 남은 자리 {free}개로 {', '.join(miss)} 역할을 모두 채울 수 없음")
            continue
        cands = [s for s in unassigned if not has_main_dup(ms, s)]
        for role, fn in ((PLANNER, can_planner), (WEB, can_web)):
            if role in miss and not any(fn(s) for s in cands):
                reasons.append(f"팀{t}에 {role}를 맡을 학생이 없음")
        if len(ms) < MIN_TEAM and len(cands) < MIN_TEAM - len(ms):
            reasons.append(f"팀{t}에 들어갈 수 있는 학생(주 역할이 겹치지 않는 학생)이 모자람")
    if not reasons:
        # 팀별로는 가능하지만 함께 만족시킬 수 없는 경우
        planners = sum(1 for s in unassigned if can_planner(s))
        webs = sum(1 for s in unassigned if can_web(s))
        need_p = sum(1 for ms in teams.values() if PLANNER in missing_required_roles(ms))
        need_w = sum(1 for ms in teams.values() if WEB in missing_required_roles(ms))
        if planners < need_p:
            reasons.append(f"기획자가 필요한 팀은 {need_p}개인데 기획자를 맡을 수 있는 팀 없는 학생은 {planners}명임")
        if webs < need_w:
            reasons.append(f"웹 프로그래머가 필요한 팀은 {need_w}개인데 웹 프로그래머를 맡을 수 있는 팀 없는 학생은 {webs}명임")
    if not reasons:
        reasons.append("팀이 없는 학생을 인원, 주 역할 중복, 필수 포함 역할 규칙을 모두 지키며 나눌 방법이 없음")
    return reasons
