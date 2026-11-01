-- 팀 편성 앱 DB 스키마 (Supabase / PostgreSQL)
-- 이 과제의 테이블 이름은 모두 tf_ 로 시작함 (6장 실습 테이블과 구분)
-- Supabase SQL 편집기에서 그대로 실행할 수 있음. 여러 번 실행해도 되도록 IF NOT EXISTS 를 씀

-- 1. 학생과 관리자
create table if not exists tf_students (
    id            bigint generated always as identity primary key,
    student_no    text not null unique,              -- 학번 (관리자는 'admin')
    name          text not null,
    main_role     text,                              -- 관리자는 비워 둠
    password_hash text not null,                     -- 비밀번호는 해시로만 저장 (평문 저장 안 함)
    is_admin      boolean not null default false,
    constraint tf_students_main_role_check check (
        main_role in ('기획자', '웹 프로그래머', '프론트엔드 디자이너', '테스터', '보고서 작성자', '발표자')
    ),
    constraint tf_students_role_required check (is_admin or main_role is not null)
);

-- 2. 보조 역할 (학생 1명에 0~2행)
create table if not exists tf_student_sub_roles (
    student_id bigint not null references tf_students(id) on delete cascade,
    role       text   not null,
    primary key (student_id, role),
    constraint tf_student_sub_roles_role_check check (
        role in ('기획자', '웹 프로그래머', '프론트엔드 디자이너', '테스터', '보고서 작성자', '발표자')
    )
);

-- 3. 팀 (팀1, 팀2, …)
create table if not exists tf_teams (
    id   bigint generated always as identity primary key,
    name text not null unique
);

-- 4. 팀원
--    (team_id, slot_no) 기본 키: 같은 자리에 두 명이 동시에 들어가면 먼저 도착한 학생만 기록됨
--    student_id 유일: 한 학생은 한 팀에만 소속됨
--    slot_no 1~4: 다섯 번째 자리를 막음
create table if not exists tf_members (
    team_id    bigint not null references tf_teams(id) on delete cascade,
    slot_no    int    not null,
    student_id bigint not null unique references tf_students(id) on delete cascade,
    primary key (team_id, slot_no),
    constraint tf_members_slot_no_check check (slot_no between 1 and 4)
);

-- 5. 지원
create table if not exists tf_applications (
    id         bigint generated always as identity primary key,
    team_id    bigint not null references tf_teams(id) on delete cascade,
    student_id bigint not null references tf_students(id) on delete cascade,
    status     text   not null default '대기',
    reason     text,                                 -- 거절 이유 등
    created_at timestamptz not null default now(),   -- UTC로 저장, 화면에는 한국 시각으로 바꿔 보여 줌
    constraint tf_applications_status_check check (status in ('대기', '승인', '거절'))
);

-- 한 학생은 결과를 기다리는 지원을 동시에 1개만 가질 수 있음
create unique index if not exists tf_applications_one_pending_per_student
    on tf_applications (student_id)
    where status = '대기';

-- 6. 투표
--    (application_id, voter_id) 기본 키: 한 팀원은 한 지원에 한 번만 투표함 (투표 바꾸기 불가)
create table if not exists tf_votes (
    application_id bigint not null references tf_applications(id) on delete cascade,
    voter_id       bigint not null references tf_students(id) on delete cascade,
    vote           text   not null,
    primary key (application_id, voter_id),
    constraint tf_votes_vote_check check (vote in ('찬성', '거부'))
);

-- 7. 설정 (마감 여부 등. 예: key = 'closed', value = 'true')
create table if not exists tf_settings (
    key   text primary key,
    value text
);

-- 8. AI 호출 기록 (몇 차 모델을 불렀는지, 성공·실패와 걸린 시간)
create table if not exists tf_ai_logs (
    id          bigint generated always as identity primary key,
    created_at  timestamptz not null default now(),
    attempt     int  not null,                       -- 1, 2, 3 (3차까지 실패하면 앱의 배정은 0)
    model       text not null,
    outcome     text not null,                       -- 성공, 실패
    detail      text,
    elapsed_sec numeric
);
