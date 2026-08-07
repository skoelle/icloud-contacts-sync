-- Schema v2: Multi-User Delta-Sync + Geburtstags-Mailer
-- Speichert alle vCard-Felder strukturiert (JSON für Mehrfachwerte) plus rohen vCard-Text als Fallback.

CREATE TABLE IF NOT EXISTS contacts (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    account             VARCHAR(100) NOT NULL,        -- Account-Name aus accounts.yml
    uid                 VARCHAR(255) NOT NULL,
    etag                VARCHAR(255) NULL,
    full_name           VARCHAR(512) NULL,
    given_name          VARCHAR(255) NULL,
    family_name         VARCHAR(255) NULL,
    middle_name         VARCHAR(255) NULL,
    prefix              VARCHAR(50)  NULL,
    suffix              VARCHAR(50)  NULL,
    nickname            VARCHAR(255) NULL,
    organization        VARCHAR(255) NULL,
    job_title           VARCHAR(255) NULL,
    department          VARCHAR(255) NULL,
    birthday            DATE NULL,
    anniversary         DATE NULL,
    notes               TEXT NULL,
    photo_base64        LONGTEXT NULL,
    photo_url           VARCHAR(2048) NULL,
    emails              JSON NULL,
    phones              JSON NULL,
    addresses           JSON NULL,
    urls                JSON NULL,
    social_profiles     JSON NULL,
    related_names       JSON NULL,
    categories          JSON NULL,
    raw_vcard           LONGTEXT NOT NULL,
    source              VARCHAR(50) NOT NULL DEFAULT 'icloud',
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    last_synced_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sync_run_id         VARCHAR(64) NULL,
    UNIQUE KEY uq_contacts_account_uid (account, uid),
    KEY idx_contacts_birthday_md (birthday)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Speichert pro Account den letzten CardDAV sync-token (RFC 6578) für Delta-Sync.
CREATE TABLE IF NOT EXISTS sync_state (
    account         VARCHAR(100) PRIMARY KEY,
    sync_token      TEXT NULL,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sync_runs (
    id              VARCHAR(64) PRIMARY KEY,
    account         VARCHAR(100) NOT NULL,
    sync_type       ENUM('initial', 'delta') NOT NULL DEFAULT 'delta',
    started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     TIMESTAMP NULL,
    status          ENUM('running', 'success', 'failed') NOT NULL DEFAULT 'running',
    contacts_upserted INT NULL,
    contacts_deleted  INT NULL,
    error_message   TEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- iCloud-Kontaktgruppen (vCards mit X-ADDRESSBOOKSERVER-KIND:group)
CREATE TABLE IF NOT EXISTS `groups` (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    account         VARCHAR(100) NOT NULL,
    uid             VARCHAR(255) NOT NULL,
    etag            VARCHAR(255) NULL,
    name            VARCHAR(512) NULL,
    raw_vcard       LONGTEXT NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    last_synced_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sync_run_id     VARCHAR(64) NULL,
    UNIQUE KEY uq_groups_account_uid (account, uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS group_members (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    group_id    INT NOT NULL,
    member_uid  VARCHAR(255) NOT NULL,
    FOREIGN KEY (group_id) REFERENCES `groups`(id) ON DELETE CASCADE,
    UNIQUE KEY uq_group_member (group_id, member_uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Protokoll der Geburtstags-Mails, verhindert Doppelversand am selben Tag.
CREATE TABLE IF NOT EXISTS birthday_mail_log (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    sent_date       DATE NOT NULL,
    contacts_count  INT NOT NULL,
    sent_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_birthday_mail_date (sent_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
