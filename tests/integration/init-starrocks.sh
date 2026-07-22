#!/usr/bin/env bash
set -euo pipefail
sleep 60  # Wait for StarRocks FE to start up properly
# Wait for StarRocks FE to be fully ready (healthcheck covers basic connectivity,
# but we also verify we can actually run DDL).
echo "==> Waiting for StarRocks to accept queries..."
for i in $(seq 1 60); do
  if mysql -h starrocks -P 9030 -u root -e "SELECT 1" &>/dev/null; then
    echo "==> StarRocks is ready."
    break
  fi
  if [ "$i" -eq 60 ]; then
    echo "ERROR: StarRocks did not become ready in time."
    exit 1
  fi
  sleep 2
done

echo "==> Creating test database and sample data..."
mysql -h starrocks -P 9030 -u root <<'SQL'
CREATE DATABASE IF NOT EXISTS test_db;
USE test_db;

CREATE TABLE IF NOT EXISTS users (
  id        INT          NOT NULL,
  name      VARCHAR(100) NOT NULL
)
DUPLICATE KEY(id)
DISTRIBUTED BY HASH(id) BUCKETS 1
PROPERTIES("replication_num" = "1");

INSERT INTO users VALUES (1, 'Alice'), (2, 'Bob'), (3, 'Charlie');
SQL

echo "==> Creating JWT-authenticated user 'testuser'..."
mysql -h starrocks -P 9030 -u root <<'SQL'
CREATE USER IF NOT EXISTS 'testuser' IDENTIFIED WITH authentication_jwt AS
'{"jwks_url":"http://keycloak:8080/realms/radiant/protocol/openid-connect/certs","principal_field":"preferred_username","required_issuer":"http://keycloak:8080/realms/radiant","required_audience":"radiant-mcp-server"}';

GRANT ALL ON ALL DATABASES TO testuser;
GRANT ALL ON ALL TABLES IN ALL DATABASES TO testuser;
SQL

echo "==> StarRocks initialisation complete."
