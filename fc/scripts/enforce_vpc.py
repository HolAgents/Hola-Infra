"""Enforce the deployed FC 3.0 function's vpcConfig via the FC 3.0 API.

`s deploy` was observed NOT to apply vpcConfig from the template (healthz
reported src=21.x — the instance had no VPC data path), and the FC 3.0
API does not know the function under the template name (FunctionNotFound),
so this script first lists the account's real FC 3.0 functions and then
updates the matching one by its ACTUAL name. Idempotent: sets the same
values every deploy.

Credentials come from the OIDC-injected ALIBABA_CLOUD_* env vars, passed
explicitly (the SDK's default credential chain came up empty in CI).
"""

import os

from alibabacloud_fc20230330.client import Client
from alibabacloud_fc20230330.models import (
    ListFunctionsRequest,
    UpdateFunctionInput,
    UpdateFunctionRequest,
    VPCConfig,
)
from alibabacloud_tea_openapi.models import Config

FUNCTION_NAME = os.environ["FC_FUNCTION_NAME"]
REGION = os.environ.get("ALIBABA_CLOUD_REGION_ID", "cn-hangzhou")


def make_client() -> Client:
    return Client(
        Config(
            region_id=REGION,
            access_key_id=os.environ["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            access_key_secret=os.environ["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            security_token=os.environ.get("ALIBABA_CLOUD_SECURITY_TOKEN", ""),
        )
    )


def resolve_target(client: Client) -> str:
    """Find the deployed function's REAL name in FC 3.0.

    The template name (FC_FUNCTION_NAME) got FunctionNotFound — print the
    whole list, then prefer an exact match, else the first name that
    starts with the template name.
    """
    listing = client.list_functions(ListFunctionsRequest())
    functions = (listing.body.functions or []) if listing.body else []
    for fn in functions:
        vpc = fn.vpc_config
        print(
            f"fc3-function: name={fn.function_name} "
            f"vpcId={vpc.vpc_id if vpc else '-'} "
            f"sg={vpc.security_group_id if vpc else '-'}"
        )
    exact = [fn.function_name for fn in functions if fn.function_name == FUNCTION_NAME]
    if exact:
        return exact[0]
    prefix = [fn.function_name for fn in functions if (fn.function_name or "").startswith(FUNCTION_NAME)]
    if prefix:
        print(f"resolved {FUNCTION_NAME} -> {prefix[0]} (prefix match)")
        return prefix[0]
    raise SystemExit(f"FATAL: no FC 3.0 function matches {FUNCTION_NAME}")


def main() -> None:
    client = make_client()
    target = resolve_target(client)
    request = UpdateFunctionRequest(
        body=UpdateFunctionInput(
            vpc_config=VPCConfig(
                vpc_id=os.environ["VPC_ID"],
                security_group_id=os.environ["SECURITY_GROUP_ID"],
                v_switch_ids=[os.environ["VSWITCH_ID"]],
            ),
        ),
    )
    response = client.update_function(target, request)
    vpc = response.body.vpc_config
    print(
        f"vpcConfig enforced on {target}: "
        f"vpcId={vpc.vpc_id} sg={vpc.security_group_id} "
        f"vSwitchIds={vpc.v_switch_ids}"
    )


if __name__ == "__main__":
    main()
