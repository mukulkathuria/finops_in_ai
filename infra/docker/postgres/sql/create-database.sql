-- psql script: creates the application database (name in psql variable :db).
SELECT format('CREATE DATABASE %I OWNER migrator', :'db')
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO migrator, app, worker, readonly_analytics', :'db') \gexec
