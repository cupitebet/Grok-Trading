CREATE TABLE tweets_raw (
    tweet_id        TEXT PRIMARY KEY,
    author_id       TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    text            TEXT NOT NULL,
    lang            TEXT,
    conversation_id TEXT,
    referenced      JSONB NOT NULL DEFAULT '[]',
    -- true jika tweet hanya disimpan sebagai konteks (induk reply/quote), bukan hasil query
    is_context      BOOLEAN NOT NULL DEFAULT false,
    raw             JSONB NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);
CREATE INDEX tweets_raw_created_at ON tweets_raw (created_at);

CREATE TABLE tweet_assets (
    tweet_id TEXT NOT NULL REFERENCES tweets_raw (tweet_id),
    asset    TEXT NOT NULL,
    PRIMARY KEY (tweet_id, asset)
);
CREATE INDEX tweet_assets_asset ON tweet_assets (asset);

CREATE TABLE tweet_metrics (
    tweet_id         TEXT NOT NULL REFERENCES tweets_raw (tweet_id),
    kind             TEXT NOT NULL CHECK (kind IN ('t0', 't60')),
    captured_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    like_count       INT,
    retweet_count    INT,
    reply_count      INT,
    quote_count      INT,
    bookmark_count   INT,
    impression_count BIGINT,
    PRIMARY KEY (tweet_id, kind)
);

CREATE TABLE accounts (
    author_id          TEXT PRIMARY KEY,
    username           TEXT NOT NULL,
    name               TEXT,
    account_created_at TIMESTAMPTZ,
    verified           BOOLEAN,
    verified_type      TEXT,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE account_snapshots (
    author_id             TEXT NOT NULL REFERENCES accounts (author_id),
    snapshot_date         DATE NOT NULL,
    followers_count       INT,
    following_count       INT,
    tweet_count           INT,
    listed_count          INT,
    description           TEXT,
    default_profile_image BOOLEAN,
    PRIMARY KEY (author_id, snapshot_date)
);

CREATE TABLE tweet_scores (
    tweet_id          TEXT NOT NULL REFERENCES tweets_raw (tweet_id),
    asset             TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_hash       TEXT NOT NULL,
    sentiment         REAL NOT NULL CHECK (sentiment BETWEEN -1 AND 1),
    confidence        REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    sarcasm           BOOLEAN NOT NULL,
    is_prediction     BOOLEAN NOT NULL,
    direction         TEXT NOT NULL CHECK (direction IN ('up', 'down', 'none')),
    horizon_days      INT,
    is_promotional    BOOLEAN NOT NULL,
    injection_attempt BOOLEAN NOT NULL,
    raw               JSONB NOT NULL,
    scored_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tweet_id, asset, model, prompt_hash)
);

CREATE TABLE score_attempts (
    tweet_id     TEXT NOT NULL REFERENCES tweets_raw (tweet_id),
    asset        TEXT NOT NULL,
    prompt_hash  TEXT NOT NULL,
    attempts     INT NOT NULL DEFAULT 0,
    last_attempt TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tweet_id, asset, prompt_hash)
);

CREATE TABLE bars (
    symbol     TEXT NOT NULL,
    interval   TEXT NOT NULL,
    open_time  TIMESTAMPTZ NOT NULL,
    close_time TIMESTAMPTZ NOT NULL,
    open       NUMERIC NOT NULL,
    high       NUMERIC NOT NULL,
    low        NUMERIC NOT NULL,
    close      NUMERIC NOT NULL,
    volume     NUMERIC NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
);

CREATE TABLE fear_greed (
    day            DATE PRIMARY KEY,
    value          SMALLINT NOT NULL CHECK (value BETWEEN 0 AND 100),
    classification TEXT NOT NULL
);

CREATE TABLE golden_labels (
    tweet_id   TEXT NOT NULL REFERENCES tweets_raw (tweet_id),
    asset      TEXT NOT NULL,
    labeler    TEXT NOT NULL,
    label      REAL NOT NULL CHECK (label BETWEEN -1 AND 1),
    note       TEXT,
    labeled_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tweet_id, asset, labeler)
);

CREATE TABLE ingest_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Rentang tweet yang terlewat karena batas halaman per run (X_MAX_PAGES).
CREATE TABLE ingest_gaps (
    id          BIGSERIAL PRIMARY KEY,
    asset       TEXT NOT NULL,
    after_id    TEXT,
    before_id   TEXT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE api_usage (
    day     DATE NOT NULL,
    service TEXT NOT NULL,
    metric  TEXT NOT NULL,
    amount  BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, service, metric)
);
