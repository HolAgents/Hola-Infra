#!/usr/bin/env bash
# Idempotent provisioning of the Hola-Infra infrastructure on a
# USER-OWNED VPC: VPC + VSwitch + SecurityGroup + NAS (for the FC
# function), and the PolarDB Serverless PostgreSQL events ledger.
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

say "NAS"
# DescribeFileSystems has no --Description filter — match client-side.
NAS_ID=$(aliyun nas DescribeFileSystems --region "$REGION" | jq -r --arg d "$NAS_DESC" '.. | objects | select(.FileSystemId? != null and (.Description? // "" | contains($d))) | .FileSystemId' | head -1)
if [ -z "$NAS_ID" ]; then
  aliyun nas CreateFileSystem --region "$REGION" \
    --FileSystemType standard --ProtocolType NFS --StorageType Capacity \
    --Description "$NAS_DESC" --ZoneId "$ZONE_ID" >/dev/null
  # DescribeFileSystems has no --Description filter — match client-side.
NAS_ID=$(aliyun nas DescribeFileSystems --region "$REGION" | jq -r --arg d "$NAS_DESC" '.. | objects | select(.FileSystemId? != null and (.Description? // "" | contains($d))) | .FileSystemId' | head -1)
  sleep 5
fi
NAS_ADDR=$(aliyun nas DescribeMountTargets --region "$REGION" --FileSystemId "$NAS_ID" | jqv '.. | objects | .MountTargetDomain? // empty')
if [ -z "$NAS_ADDR" ]; then
  aliyun nas CreateMountTarget --region "$REGION" --FileSystemId "$NAS_ID" \
    --NetworkType Vpc --VpcId "$VPC_ID" --VSwitchId "$VSW_ID" \
    --AccessGroupName DEFAULT_VPC_GROUP_NAME >/dev/null
  for i in $(seq 1 30); do
    NAS_ADDR=$(aliyun nas DescribeMountTargets --region "$REGION" --FileSystemId "$NAS_ID" | jqv '.. | objects | .MountTargetDomain? // empty')
    [ -n "$NAS_ADDR" ] && break
    sleep 5
  done
fi
echo "nas=$NAS_ID addr=$NAS_ADDR"

# ---------------------------------------------------------------------------
# 3. PolarDB Serverless cluster + account + databases
# ---------------------------------------------------------------------------

say "Find or create cluster"
CLUSTER_ID=$(aliyun polardb DescribeDBClusters --region "$REGION" --DBClusterDescription "$CLUSTER_DESC" | jqv '.. | objects | .DBClusterId? // empty')
if [ -z "$CLUSTER_ID" ]; then
  PW=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 20)
  echo "::add-mask::$PW"
  echo "$PW" > /tmp/hola_db_pw
  # ZoneId omitted: VSwitchId implies the zone and the service picks a
  # serverless-capable one (InvalidZoneID.NotFound otherwise).
  aliyun polardb CreateDBCluster --region "$REGION" \
    --DBType PostgreSQL --DBVersion 14 --PayType Postpaid \
    --ServerlessType AgileServerless --ScaleMin 1 --ScaleMax 8 \
    --ScaleRoNumMin 1 --ScaleRoNumMax 1 \
    --DBNodeClass polar.pg.sl.small \
    --DBClusterDescription "$CLUSTER_DESC" \
    --VPCId "$VPC_ID" --VSwitchId "$VSW_ID"
  CLUSTER_ID=$(aliyun polardb DescribeDBClusters --region "$REGION" --DBClusterDescription "$CLUSTER_DESC" | jqv '.. | objects | .DBClusterId? // empty')
  say "Waiting for cluster $CLUSTER_ID to become Running"
  ST=""
  for i in $(seq 1 60); do
    ST=$(aliyun polardb DescribeDBClusters --region "$REGION" --DBClusterDescription "$CLUSTER_DESC" | jqv '.. | objects | .DBClusterStatus? // empty')
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
  --SecurityIps "10.10.0.0/16" --DBClusterIPArrayName default

say "Endpoint"
ATTR=$(aliyun polardb DescribeDBClusterAttribute --region "$REGION" --DBClusterId "$CLUSTER_ID")
DB_HOST=$(echo "$ATTR" | jqv '.. | objects | .Address? // empty')
[ -n "$DB_HOST" ] || DB_HOST="${CLUSTER_ID}.polardb.rds.aliyuncs.com"

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
echo "VSWITCH_ID=$VSW_ID"
echo "SECURITY_GROUP_ID=$SG_ID"
echo "NAS_SERVER_ADDR=${NAS_ADDR}:/"
echo "========================================================================"
