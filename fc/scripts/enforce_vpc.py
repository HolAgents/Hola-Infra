"""Enforce the deployed FC 2.0 service's vpcConfig via the FC 2.0 API.

The functions are FC 2.0 (service$function), where VPC is a SERVICE-level
setting. `s deploy` did not apply the template's vpcConfig — it was placed
at function level, which the FC 2.0 schema silently drops (healthz showed
a public source IP, i.e. no VPC data path). The deploy jobs call this
explicitly after `s deploy`. Idempotent: sets the same values every run.

Credentials come from the OIDC-injected ALIBABA_CLOUD_* env vars, passed
explicitly (the SDK's default credential chain came up empty in CI).
"""

import os

from alibabacloud_fc_open20210406.client import Client
from alibabacloud_fc_open20210406.models import UpdateServiceRequest, VPCConfig
from alibabacloud_tea_openapi.models import Config

SERVICE_NAME = os.environ["FC_SERVICE_NAME"]
REGION = os.environ.get("ALIBABA_CLOUD_REGION_ID", "cn-hangzhou")


def main() -> None:
    # FC 2.0 endpoints embed the account id; the deploy secret provides it.
    endpoint = None
    if os.environ.get("FC_ACCOUNT_ID"):
        endpoint = f"{os.environ['FC_ACCOUNT_ID']}.{REGION}.fc.aliyuncs.com"
    client = Client(
        Config(
            region_id=REGION,
            access_key_id=os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            access_key_secret=os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            security_token=os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN", ""),
            endpoint=endpoint,
        )
    )
    request = UpdateServiceRequest(
        vpc_config=VPCConfig(
            vpc_id=os.environ["VPC_ID"],
            security_group_id=os.environ["SECURITY_GROUP_ID"],
            v_switch_ids=[os.environ["VSWITCH_ID"]],
        ),
    )
    response = client.update_service(SERVICE_NAME, request)
    vpc = response.body.vpc_config
    print(
        f"vpcConfig enforced on service {SERVICE_NAME}: "
        f"vpcId={vpc.vpc_id} sg={vpc.security_group_id} "
        f"vSwitchIds={vpc.v_switch_ids}"
    )


if __name__ == "__main__":
    main()
