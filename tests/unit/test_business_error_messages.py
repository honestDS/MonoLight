import pytest

from app.core.exceptions import AuthException, ForbiddenException, LLMException, ParameterException, ServerException
from app.core.i18n.context import reset_current_locale, set_current_locale


@pytest.mark.parametrize(
    ("exception_type", "code", "messages"),
    [
        (AuthException, 401, {"zh": "无效的身份凭证", "en": "Invalid credentials"}),
        (ForbiddenException, 403, {"zh": "无权操作此会话", "en": "No permission to operate on this session"}),
        (ParameterException, 400, {"zh": "参数验证失败", "en": "Parameter validation failed"}),
        (ServerException, 500, {"zh": "系统内部错误", "en": "Internal server error"}),
        (
            LLMException,
            502,
            {
                "zh": "大模型接口调用发生非预期异常",
                "en": "Unexpected exception occurred when calling the large model interface",
            },
        ),
    ],
)
def test_business_exception_to_response_is_localized(exception_type, code, messages):
    data = {"field": "uid"}
    exception = exception_type(data=data)

    for locale, expected_message in messages.items():
        locale_token = set_current_locale(locale)
        try:
            assert exception.to_response() == {"code": code, "message": expected_message, "data": data}
        finally:
            reset_current_locale(locale_token)


def test_business_exception_to_response_uses_render_locale_and_restores_context():
    data = {"field": "uid"}
    locale_token = set_current_locale("zh")
    try:
        exception = AuthException(data=data)

        switched_locale_token = set_current_locale("en")
        try:
            assert exception.to_response() == {"code": 401, "message": "Invalid credentials", "data": data}
        finally:
            reset_current_locale(switched_locale_token)

        assert exception.to_response() == {"code": 401, "message": "无效的身份凭证", "data": data}
    finally:
        reset_current_locale(locale_token)
