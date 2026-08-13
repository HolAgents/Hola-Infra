#!/usr/bin/env bash
# Idempotent provisioning of the Hola-Infra database on a
# USER-OWNED VPC: VPC + VSwitch + SecurityGroup + the PolarDB
# Serverless PostgreSQL events ledger (no NAS — the SQLite fallback
# writes to the instance's ephemeral /tmp).
# Runs in CI with OIDC STS credentials; the aliyun CLI must already be
# configured (StsToken mode).
#
# The FC deploy component's DefaultNas lives in an FC-MANAGED VPC that
# user resources cannot join (InvalidVPC.Malformed), so this script
# provisions a dedicated user VPC and prints the ids that s.yaml needs.
set -euo pipefail

REGION=${REGION:-cn-hangzhou}
CLUSTER_DESC=hola-events-ledger
DB_USER=${DB_USER:-hola}
DB_PROD=hola
DB_STAGING=hola_staging
VPC_NAME=hola-infra-vpc
VSW_NAME=hola-infra-vsw
SG_NAME=hola-infra-sg
NAS_DESC=hola-infra-nas

say() { echo "== $* =="; }

jqv() { # jqv '<filter>' — first matching value anywhere in the JSON tree
  jq -r "$1 | select(. != null and . != \"\")" | head -1
}

# ---------------------------------------------------------------------------
# 1. User-owned VPC / VSwitch / SecurityGroup (idempotent lookups first)
# ---------------------------------------------------------------------------

say "User VPC"
VPC_ID=$(aliyun vpc DescribeVpcs --region "$REGION" --VpcName "$VPC_NAME" | jqv '.. | objects | .VpcId? // empty')
if [ -z "$VPC_ID" ]; then
  aliyun vpc CreateVpc --region "$REGION" --VpcName "$VPC_NAME" --CidrBlock 10.10.0.0/16 >/dev/null
  VPC_ID=$(aliyun vpc DescribeVpcs --region "$REGION" --VpcName "$VPC_NAME" | jqv '.. | objects | .VpcId? // empty')
fi
echo "vpc=$VPC_ID"

ZONE_ID=$(aliyun ecs DescribeZones --region "$REGION" | jqv '.. | objects | .ZoneId? // empty')
VSW_ID=$(aliyun vpc DescribeVSwitches --region "$REGION" --VpcId "$VPC_ID" | jqv '.. | objects | .VSwitchId? // empty')
if [ -z "$VSW_ID" ]; then
  aliyun vpc CreateVSwitch --region "$REGION" \
    --VpcId "$VPC_ID" --ZoneId "$ZONE_ID" --CidrBlock 10.10.0.0/24 --VSwitchName "$VSW_NAME" >/dev/null
  VSW_ID=$(aliyun vpc DescribeVSwitches --region "$REGION" --VpcId "$VPC_ID" | jqv '.. | objects | .VSwitchId? // empty')
fi
echo "vswitch=$VSW_ID zone=$ZONE_ID"

SG_ID=$(aliyun ecs DescribeSecurityGroups --region "$REGION" --VpcId "$VPC_ID" --SecurityGroupName "$SG_NAME" | jqv '.. | objects | .SecurityGroupId? // empty')
if [ -z "$SG_ID" ]; then
  aliyun ecs CreateSecurityGroup --region "$REGION" --VpcId "$VPC_ID" --SecurityGroupName "$SG_NAME" >/dev/null
  SG_ID=$(aliyun ecs DescribeSecurityGroups --region "$REGION" --VpcId "$VPC_ID" --SecurityGroupName "$SG_NAME" | jqv '.. | objects | .SecurityGroupId? // empty')
fi
echo "security_group=$SG_ID"

# ---------------------------------------------------------------------------
# 2. NAS for the FC function (user-owned, NFS, mounted at /mnt/auto)
# ---------------------------------------------------------------------------

say "Find or create cluster"
CLUSTER_ID=$(aliyun polardb DescribeDBClusters --region "$REGION" | jq -r --arg d "$CLUSTER_DESC" '.. | objects | select(.DBClusterId? != null and (.DBClusterDescription? // "" == $d)) | .DBClusterId' | head -1)

create_cluster() {
  PW=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 20)
  echo "::add-mask::$PW"
  echo "$PW" > /tmp/hola_db_pw
  # PolarDB Serverless PG is not available in every zone — iterate over
  # the account's zones, creating a VSwitch per zone, until one accepts
  # the cluster. (Cross-zone VPC routing keeps the FC function able to
  # reach the internal endpoint regardless.)
  N=1
  DB_VSW_ID=""
  DB_ZONE=""
  for Z in $(aliyun ecs DescribeZones --region "$REGION" | jq -r '.. | objects | .ZoneId? // empty' | sort -u); do
    ZVSW=$(aliyun vpc DescribeVSwitches --region "$REGION" --VpcId "$VPC_ID" --ZoneId "$Z" | jqv '.. | objects | .VSwitchId? // empty')
    if [ -z "$ZVSW" ]; then
      aliyun vpc CreateVSwitch --region "$REGION" --VpcId "$VPC_ID" --ZoneId "$Z" \
        --CidrBlock "10.10.$N.0/24" --VSwitchName "${VSW_NAME}-$Z" >/dev/null 2>&1 || { N=$((N+1)); continue; }
      ZVSW=$(aliyun vpc DescribeVSwitches --region "$REGION" --VpcId "$VPC_ID" --ZoneId "$Z" | jqv '.. | objects | .VSwitchId? // empty')
    fi
    echo "trying zone $Z (vswitch $ZVSW)"
    # Capture the cluster id DIRECTLY from the create response.  Run 11
    # discarded it and re-looked-up via DescribeDBClusters immediately
    # after creation — the async create wasn't visible yet, the lookup
    # returned empty, and the account step fired with a blank id
    # (InvalidDBClusterId.Malformed), orphaning the new cluster.
    if CREATE_RESP=$(aliyun polardb CreateDBCluster --region "$REGION" \
        --DBType PostgreSQL --DBVersion 14 --PayType Postpaid \
        --ServerlessType AgileServerless --ScaleMin 1 --ScaleMax 8 \
        --ScaleRoNumMin 1 --ScaleRoNumMax 1 \
        --DBNodeClass polar.pg.sl.small \
        --DBClusterDescription "$CLUSTER_DESC" \
        --VPCId "$VPC_ID" --VSwitchId "$ZVSW" 2>&1); then
      CLUSTER_ID=$(echo "$CREATE_RESP" | jq -r '.. | objects | .DBClusterId? // empty' 2>/dev/null | head -1 || echo "")
    fi
    if [ -n "$CLUSTER_ID" ]; then
      echo "cluster accepted in zone $Z: $CLUSTER_ID"
      DB_VSW_ID="$ZVSW"; DB_ZONE="$Z"
      break
    fi
    echo "zone $Z rejected"
    N=$((N+1))
  done
  [ -n "$DB_VSW_ID" ] || { echo "FATAL: no zone accepted serverless PG"; exit 1; }
  say "Waiting for cluster $CLUSTER_ID to become Running"
  ST=""
  for i in $(seq 1 60); do
    RESP=$(aliyun polardb DescribeDBClusters --region "$REGION")
    ST=$(echo "$RESP" | jq -r --arg d "$CLUSTER_DESC" '.. | objects | select(.DBClusterDescription? == $d) | .DBClusterStatus // empty' | head -1)
    # Fallback: if the create response carried no id for any reason, the
    # cluster is visible in Describe by the time it's Running.
    if [ -z "$CLUSTER_ID" ]; then
      CLUSTER_ID=$(echo "$RESP" | jq -r --arg d "$CLUSTER_DESC" '.. | objects | select(.DBClusterId? != null and (.DBClusterDescription? // "" == $d)) | .DBClusterId' | head -1)
    fi
    [ "$ST" = "Running" ] && break
    sleep 15
  done
  [ -n "$CLUSTER_ID" ] || { echo "FATAL: cluster id never resolved"; exit 1; }
  [ "$ST" = "Running" ] || { echo "FATAL: cluster not running (status=$ST)"; exit 1; }
}

if [ -z "$CLUSTER_ID" ]; then
  create_cluster
else
  say "Cluster exists: $CLUSTER_ID"
  PW=$(cat /tmp/hola_db_pw 2>/dev/null || echo "")
  if [ -z "$PW" ]; then
    # Orphan cluster from an interrupted run: the password only lives in
    # the creating run's runner temp file. The ledger is empty — delete
    # and recreate so the password and account are created together.
    say "No stored password for existing cluster — recreating"
    # Delete EVERY orphan (repeated failed runs can leave several) —
    # deleting only the first match would leave leftovers that poison
    # later lookups.
    for ORPHAN_ID in $(aliyun polardb DescribeDBClusters --region "$REGION" | jq -r --arg d "$CLUSTER_DESC" '.. | objects | select(.DBClusterId? != null and (.DBClusterDescription? // "" == $d)) | .DBClusterId'); do
      say "Deleting orphan cluster $ORPHAN_ID"
      aliyun polardb DeleteDBCluster --region "$REGION" --DBClusterId "$ORPHAN_ID" >/dev/null
    done
    for i in $(seq 1 30); do
      GONE=$(aliyun polardb DescribeDBClusters --region "$REGION" | jq -r --arg d "$CLUSTER_DESC" '.. | objects | select(.DBClusterId? != null and (.DBClusterDescription? // "" == $d)) | .DBClusterId' | head -1)
      [ -z "$GONE" ] && break
      sleep 10
    done
    [ -z "$GONE" ] || { echo "FATAL: orphan cluster still present after delete"; exit 1; }
    CLUSTER_ID=""
    create_cluster
  fi
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
    say "Creating database $DB"
    # PolarDB PostgreSQL requires Collate/Ctype whenever CharacterSetName
    # is specified (server-side InvalidParameters.Format otherwise).
    aliyun polardb CreateDatabase --region "$REGION" --DBClusterId "$CLUSTER_ID" \
      --DBName "$DB" --AccountName "$DB_USER" \
      --CharacterSetName UTF8 --Collate C --Ctype C
  fi
done

say "Whitelist (VPC CIDR)"
aliyun polardb ModifyDBClusterAccessWhitelist --region "$REGION" --DBClusterId "$CLUSTER_ID" \
  --SecurityIps "10.10.0.0/16" --DBClusterIPArrayName default

say "Endpoint"
# DescribeDBClusterEndpoints is the API that actually returns the
# connection address; DescribeDBClusterAttribute may not.  Print the raw
# responses so a failure here is diagnosable from the run log, and
# hard-FATAL instead of guessing a hostname that may not resolve.
EP_RAW=$(aliyun polardb DescribeDBClusterEndpoints --region "$REGION" --DBClusterId "$CLUSTER_ID" 2>&1 || echo "")
echo "endpoints-raw: $EP_RAW"
DB_HOST=$(echo "$EP_RAW" | jq -r '.. | objects | (.Address? // .ConnectionString? // empty)' 2>/dev/null | head -1 || echo "")
if [ -z "$DB_HOST" ]; then
  EP_RAW=$(aliyun polardb DescribeDBClusterAttribute --region "$REGION" --DBClusterId "$CLUSTER_ID" 2>&1 || echo "")
  echo "attr-raw: $EP_RAW"
  DB_HOST=$(echo "$EP_RAW" | jq -r '.. | objects | (.Address? // .ConnectionString? // empty)' 2>/dev/null | head -1 || echo "")
fi
[ -n "$DB_HOST" ] || { echo "FATAL: no endpoint address found"; exit 1; }

echo ""
echo "=========================== CONNECTION BLOCK ==========================="
echo "DB_HOST=$DB_HOST"
echo "DB_PORT=5432"
echo "DB_USER=$DB_USER"
echo "DB_PASSWORD=$PW"
echo "DB_NAME=$DB_PROD"
echo "DB_NAME_STAGING=$DB_STAGING"
echo "--- s.yaml infrastructure wiring ---"
echo "VPC_ID=$VPC_ID"
echo "DB_ZONE=$DB_ZONE"
echo "DB_VSWITCH_ID=$DB_VSW_ID"
echo "VSWITCH_ID=$VSW_ID"
echo "SECURITY_GROUP_ID=$SG_ID"
echo "========================================================================"
