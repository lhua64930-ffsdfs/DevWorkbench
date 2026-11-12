-- Apply to a restored database before reconnecting application traffic.
-- Restore as codepilot_migrator so migrations retain object ownership.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO codepilot_migrator;
GRANT USAGE ON SCHEMA public TO codepilot;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO codepilot;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO codepilot;
ALTER DEFAULT PRIVILEGES FOR ROLE codepilot_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO codepilot;
ALTER DEFAULT PRIVILEGES FOR ROLE codepilot_migrator IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO codepilot;
