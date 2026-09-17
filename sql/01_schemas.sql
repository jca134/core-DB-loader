-- Top-level schemas for the filovirus integration database.
-- Run this first. "raw" already exists if load_bvbrc.py has run before.

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS ops;
-- analytics intentionally omitted per current scope.
