"""Enforce the deployed FC 3.0 function's vpcConfig via the FC 3.0 API.

`s deploy` was observed NOT to apply vpcConfig from the template (healthz
reported src=21.0.17.103 — the instance had no VPC data path), so the
deploy jobs call this explicitly after `s deploy`. Idempotent: sets the
same values every deploy.

Credentials come from the OIDC-injected ALIBABA_CLOUD_* env vars
(the SDK reads them automatically).
"""

import os

from alibabacloud_fc20230330.client import Client
from alibabacloud_fc20230330.models import (
    UpdateFunctionInput,
    UpdateFunctionRequest,
    VPCConfig,
)
from alibabacloud_tea_openapi.models import Config

FUNCTION_NAME = os.environ["FC_FUNCTION_NAME"]
REGION = os.environ.get("ALIBABA_CLOUD_REGION_ID", "cn-hangzhou")


def main() -> None:
    # Pass the OIDC-injected credentials explicitly — the SDK's default
    # credential chain came up empty (InvalidCredentials) in CI.
    client = Client(
        Config(
            region_id=REGION,
            access_key_id=os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            access_key_secret=os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            security_token=os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN", ""),
        )
    )
    # UpdateFunctionRequest wraps the actual config in `body`
    # (UpdateFunctionInput) — the FC 3.0 SDK shape, verified via
    # inspect.signature on v4.7.9.
    request = UpdateFunctionRequest(
        body=UpdateFunctionInput(
            vpc_config=VPCConfig(
                vpc_id=os.environ["VPC_ID"],
                security_group_id=os.environ["SECURITY_GROUP_ID"],
                v_switch_ids=[os.environ["VSWITCH_ID"]],
            ),
        ),
    )
    response = client.update_function(FUNCTION_NAME, request)
    vpc = response.body.vpc_config
    print(
        f"vpcConfig enforced on {FUNCTION_NAME}: "
        f"vpcId={vpc.vpc_id} sg={vpc.security_group_id} "
        f"vSwitchIds={vpc.v_switch_ids}"
    )


if __name__ == "__main__":
    main()
