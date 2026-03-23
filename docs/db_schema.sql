CREATE TABLE users (
    id bigserial primary key,
    telegram_id bigint unique not null,
    username varchar(128),
    first_name varchar(128),
    age smallint check (age >= 18),
    gender varchar(20),
    city varchar(100),
    created_at timestamptz default now()
);

CREATE TABLE profiles (
    id bigserial primary key,
    user_id bigint references users(id) on delete cascade,
    bio text,
    interests jsonb,
    photo_urls text[],
    completeness_score numeric(5,4) default 0,
    created_at timestamptz default now()
);

CREATE TABLE preferences (
    id bigserial primary key,
    user_id bigint references users(id) on delete cascade,
    age_min smallint default 18,
    age_max smallint default 99,
    gender_pref varchar(20),
    city_pref varchar(100)
);

CREATE TABLE ratings (
    id bigserial primary key,
    profile_id bigint references profiles(id) on delete cascade,
    primary_score numeric(6,2) default 0,
    behavioral_score numeric(6,2) default 0,
    combined_score numeric(6,2) default 0,
    updated_at timestamptz default now()
);

CREATE TABLE interactions (
    id bigserial primary key,
    actor_user_id bigint references users(id) on delete cascade,
    target_profile_id bigint references profiles(id) on delete cascade, 
    action varchar(20) not null check (action in ('like', 'pass', 'super_like')),
    is_match boolean default false, 
    created_at timestamptz default now()
);