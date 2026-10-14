import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.mark.parametrize(
    ("overrides", "invalid_field"),
    [
        ({"emp_code": "string"}, "emp_code"),
        ({"emp_code": "EMP0001", "joined_on": "string"}, "joined_on"),
    ],
)
def test_create_employee_rejects_invalid_contract_fields(overrides, invalid_field):
    payload = {
        "emp_code": "EMP0001",
        "name": "Test Employee",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-10-13",
        **overrides,
    }

    response = TestClient(main.app).post("/employees", json=payload)

    assert response.status_code == 422
    assert any(error["loc"][-1] == invalid_field for error in response.json()["detail"])


def test_department_summary_is_employee_rooted_aggregation():
    pipeline = main.department_summary_pipeline(*main.valid_month("2026-10"))

    assert pipeline[0] == {"$match": {"joined_on": {"$lte": "2026-10-31"}}}
    assert pipeline[1]["$lookup"]["from"] == "attendance_logs"
    assert pipeline[2]["$unwind"]["preserveNullAndEmptyArrays"] is True