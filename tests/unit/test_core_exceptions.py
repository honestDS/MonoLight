import pytest
from app.core.exceptions import (
    BaseBusinessException, AuthException, ForbiddenException,
    ResourceNotFoundException, ParameterException, ServerException, LLMException
)

def test_base_business_exception():
    exc = BaseBusinessException(code=400, message="test error", data={"key": "val"})
    assert exc.code == 400
    assert exc.message == "test error"
    assert exc.data == {"key": "val"}
    assert str(exc) == "test error"

def test_specific_exceptions():
    assert AuthException().code == 401
    assert ForbiddenException().code == 403
    assert ResourceNotFoundException().code == 404
    assert ParameterException().code == 400
    assert ServerException().code == 500
    assert LLMException().code == 502
    
    # 验证自定义消息
    assert AuthException(message="custom auth").message == "custom auth"
