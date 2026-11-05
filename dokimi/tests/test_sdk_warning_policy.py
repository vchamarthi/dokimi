# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Only observed SDK TypedDict advisories bypass the project's warnings-as-errors policy."""

import re
import warnings

import pytest

SCHEMA_MODULE = "pydantic._internal._generate_schema"
ADVISORIES = {
    '_CopilotEndpointsPayload': ("Item 'api' on TypedDict class '_CopilotEndpointsPayload' is using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
    'ChatCompletionRedactedThinkingBlock': ("Item 'index' on TypedDict class 'ChatCompletionRedactedThinkingBlock' is using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
    'ChatCompletionThinkingBlock': ("Item 'index' on TypedDict class 'ChatCompletionThinkingBlock' is using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
    '_GithubUserPayload': ("Item 'login' on TypedDict class '_GithubUserPayload' is using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
    'ChatCompletionReasoningItem': ("Item 'summary' on TypedDict class 'ChatCompletionReasoningItem' is using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
    '_AccessTokenPoll': ("Items 'access_token', 'error' on TypedDict class '_AccessTokenPoll' are using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
    '_AccessTokenPollPayload': ("Items 'access_token', 'error', 'error_description', 'interval' on TypedDict class '_AccessTokenPollPayload' are using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
    '_DeviceCode': ("Items 'device_code', 'user_code', 'verification_uri' on TypedDict class '_DeviceCode' are using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
    '_DeviceCodePayload': ("Items 'device_code', 'user_code', 'verification_uri', 'expires_in', 'interval' on TypedDict class '_DeviceCodePayload' are using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
    '_CopilotTokenPayload': ("Items 'token', 'expires_at', 'endpoints' on TypedDict class '_CopilotTokenPayload' are using the `ReadOnly` qualifier."
        ' Pydantic will not protect items from any mutation on dictionary instances.'),
}


@pytest.mark.parametrize("message", ADVISORIES.values(), ids=ADVISORIES)
def test_known_sdk_readonly_advisory_does_not_interrupt_model_import(message):
    with warnings.catch_warnings(record=True) as emitted:
        warnings.warn_explicit(message, UserWarning, filename="sdk.py", lineno=1, module=SCHEMA_MODULE)
    assert emitted == []


@pytest.mark.parametrize("message", ADVISORIES.values(), ids=ADVISORIES)
def test_unknown_sdk_class_still_fails_the_test(message):
    unknown = re.sub(r"TypedDict class '[^']+'", "TypedDict class 'UnknownSDKClass'", message)
    with warnings.catch_warnings(), pytest.raises(UserWarning):
        warnings.warn_explicit(unknown, UserWarning, filename="sdk.py", lineno=1, module=SCHEMA_MODULE)


@pytest.mark.parametrize("message", ADVISORIES.values(), ids=ADVISORIES)
def test_unknown_sdk_field_still_fails_the_test(message):
    unknown = re.sub(r"'[^']+'", "'unknown_field'", message, count=1)
    with warnings.catch_warnings(), pytest.raises(UserWarning):
        warnings.warn_explicit(unknown, UserWarning, filename="sdk.py", lineno=1, module=SCHEMA_MODULE)


@pytest.mark.parametrize(("message", "module", "category"), [
    ("A different warning from the same schema generator", SCHEMA_MODULE, UserWarning),
    (ADVISORIES["_DeviceCode"], "dokimi.example", UserWarning),
    (ADVISORIES["_DeviceCode"], SCHEMA_MODULE + ".unexpected", UserWarning),
    (ADVISORIES["_DeviceCode"], SCHEMA_MODULE, DeprecationWarning),
])
def test_other_sdk_warning_text_modules_and_categories_still_fail(message, module, category):
    with warnings.catch_warnings(), pytest.raises(category):
        warnings.warn_explicit(message, category, filename="sdk.py", lineno=1, module=module)
