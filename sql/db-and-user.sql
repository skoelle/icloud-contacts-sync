#!mariadb
-- Kontakte-Sync MariaDB Setup Script
-- Auszufuehren einmalig auf dem MariaDB LXC, z.B. via:
-- mysql -u root -p < sql/db-and-user.sql

CREATE DATABASE IF NOT EXISTS contacts CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'contacts_sync'@'%' IDENTIFIED BY 'HIER_SICHERES_PASSWORT';

GRANT ALL PRIVILEGES ON contacts.* TO 'contacts_sync'@'%';

FLUSH PRIVILEGES;