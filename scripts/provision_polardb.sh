#!/usr/bin/env bash
# Idempotent provisioning of the Hola-Infra events ledger on PolarDB
# Serverless (PostgreSQL). Runs in CI with OIDC STS credentials; the
# aliyun CLI must already be configured (StsToken mode).
#
# Discovers the FC service's VPC from its NAS mount, creates (or reuses)
# the cluster, account, and databases, and prints the connection block.
set -euo pipefail

REGION=${REGION:-cn-hangzhou}
CLUSTER_DESC=hola-events-ledger
DB_USER=${DB_USER:-hola}
DB_PROD=hola
DB_STAGING=hola_staging

say() { echo "== $* =="; }

say "Discover VPC from NAS file systems"
NAS=$(aliyun nas DescribeFileSystems --region "$REGION")
# Each file system object nests its mount targets (which carry VpcId).
# Print the fs↔vpc mapping, then prefer the FC deploy component's
# default NAS ("DefaultNas" in the description).
echo "$NAS" | jq -r '
  .. | objects | select(has("FileSystemId") and has("MountTargets"))
  | . as $fs
  | ($fs.MountTargets.MountTarget[]? // {}) as $mt
  | "fs=\($fs.FileSystemId) vpc=\($mt.VpcId // "-") vsw=\($mt.VSwitchId // "-") desc=\($fs.Description // "-")"' | sort -u

FC_FS_ID=$(echo "$NAS" | jq -r '
  .. | objects | select(.FileSystemId? != null and (.Description? // "" | contains("DefaultNas")))
  | .FileSystemId' | head -1)
if [ -n "$FC_FS_ID" ]; then
  VPC_ID=$(echo "$NAS" | jq -r --arg fs "$FC_FS_ID" '
    .. | objects | select(.FileSystemId? == $fs)
    | .MountTargets.MountTarget[]?.VpcId // empty' | head -1)
else
  VPC_ID=""
fi

# Cross-check with the FC API when the CLI supports it (best-effort).
FC_API_VPC=$(aliyun fc3 DescribeService --region "$REGION" --ServiceName hola-webhook-service 2>/dev/null | jq -r '.. | objects | .VpcId? // empty' | head -1 || true)
echo "fc-api vpc: ${FC_API_VPC:-unavailable}"
if [ -n "$FC_API_VPC" ] && [ "$FC_API_VPC" != "null" ]; then
  VPC_ID="$FC_API_VPC"
fi

test -n "$VPC_ID" || { echo "FATAL: could not determine the FC service VPC"; exit 1; }
say "Using VPC: $VPC_ID"

VPC=$(aliyun vpc DescribeVpcs --region "$REGION" --VpcId "$VPC_ID")
VPC_CIDR=$(echo "$VPC" | jq -r '.. | objects | .CidrBlock? // empty' | head -1)
VSW=$(aliyun vpc DescribeVSwitches --region "$REGION" --VpcId "$VPC_ID")
VSW_ID=$(echo "$VSW" | jq -r '.. | objects | .VSwitchId? // empty' | head -1)
ZONE_ID=$(echo "$VSW" | jq -r '.. | objects | .ZoneId? // empty' | head -1)
echo "cidr=$VPC_CIDR vswitch=$VSW_ID zone=$ZONE_ID"

say "Find or create cluster"
CLUSTER_ID=$(aliyun polardb DescribeDBClusters --region "$REGION" --DBClusterDescription "$CLUSTER_DESC" | jq -r '.. | objects | .DBClusterId? // empty' | head -1)
if [ -z "$CLUSTER_ID" ]; then
  PW=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 20)
  echo "::add-mask::$PW"
  echo "$PW" > /tmp/hola_db_pw
  # Dump the parameter schema for debugging future API drift.
  aliyun polardb CreateDBCluster --help 2>&1 | grep -iE "serverless|scale|dbnodeclass" | head -20 || true
  aliyun polardb CreateDBCluster --region "$REGION" \
    --DBType PostgreSQL --DBVersion 14 --PayType Postpaid \
    --ServerlessType AgileServerless --ScaleMin 1 --ScaleMax 8 \
    --ScaleRoNumMin 1 --ScaleRoNumMax 1 \
    --DBNodeClass polar.pg.sl.small \
    --DBClusterDescription "$CLUSTER_DESC" \
    --ZoneId "$ZONE_ID" --VPCId "$VPC_ID" --VSwitchId "$VSW_ID"
  CLUSTER_ID=$(aliyun polardb DescribeDBClusters --region "$REGION" --DBClusterDescription "$CLUSTER_DESC" | jq -r '.. | objects | .DBClusterId? // empty' | head -1)
  say "Waiting for cluster $CLUSTER_ID to become Running"
  for i in $(seq 1 60); do
    ST=$(aliyun polardb DescribeDBClusters --region "$REGION" --DBClusterDescription "$CLUSTER_DESC" | jq -r '.. | objects | .DBClusterStatus? // empty' | head -1)
    [ "$ST" = "Running" ] && break
    sleep 15
  done
  [ "$ST" = "Running" ] || { echo "FATAL: cluster not running (status=$ST)"; exit 1; }
else
  say "Cluster exists: $CLUSTER_ID"
  PW=$(cat /tmp/hola_db_pw 2>/dev/null || echo "")
fi
echo "cluster=$CLUSTER_ID"

say "Account"
if ! aliyun polardb DescribeAccounts --region "$REGION" --DBClusterId "$CLUSTER_ID" | jq -e '.. | objects | select(.AccountName? == "'"$DB_USER"'")' >/dev/null 2>&1; then
  [ -n "$PW" ] || { echo "FATAL: account missing but no stored password for reuse"; exit 1; }
  aliyun polardb CreateAccount --region "$REGION" --DBClusterId "$CLUSTER_ID" \
    --AccountName "$DB_USER" --AccountPassword "$PW" --AccountType Super
fi

say "Databases"
for DB in "$DB_PROD" "$DB_STAGING"; do
  if ! aliyun polardb DescribeDatabases --region "$REGION" --DBClusterId "$CLUSTER_ID" | jq -e '.. | objects | select(.DBName? == "'"$DB"'")' >/dev/null 2>&1; then
    aliyun polardb CreateDatabase --region "$REGION" --DBClusterId "$CLUSTER_ID" \
      --DBName "$DB" --AccountName "$DB_USER"
  fi
done

say "Whitelist (VPC CIDR)"
aliyun polardb ModifyDBClusterAccessWhitelist --region "$REGION" --DBClusterId "$CLUSTER_ID" \
  --SecurityIps "$VPC_CIDR" --DBClusterIPArrayName default

say "Endpoint"
ATTR=$(aliyun polardb DescribeDBClusterAttribute --region "$REGION" --DBClusterId "$CLUSTER_ID")
DB_HOST=$(echo "$ATTR" | jq -r '.. | objects | .Address? // empty' | head -1)
[ -n "$DB_HOST" ] || DB_HOST="${CLUSTER_ID}.polardb.rds.aliyuncs.com"

echo ""
echo "=========================== CONNECTION BLOCK ==========================="
echo "DB_HOST=$DB_HOST"
echo "DB_PORT=5432"
echo "DB_USER=$DB_USER"
echo "DB_PASSWORD=$PW"
echo "DB_NAME=$DB_PROD"
echo "DB_NAME_STAGING=$DB_STAGING"
echo "========================================================================"
