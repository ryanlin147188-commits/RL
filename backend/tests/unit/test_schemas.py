"""Unit tests for Pydantic schemas — validation, defaults, coercion."""
from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.schemas.schedule import ScheduleCreate, ScheduleResponse, ScheduleUpdate


# ── ScheduleCreate ────────────────────────────────────────────────────


class TestScheduleCreate:
    _valid_dt = datetime(2026, 6, 1, 9, 0, 0)

    def _valid(self, **overrides):
        defaults = {
            "name": "Daily smoke",
            "next_run_at": self._valid_dt,
        }
        defaults.update(overrides)
        return ScheduleCreate(**defaults)

    def test_minimal_fields_valid(self):
        s = self._valid()
        assert s.name == "Daily smoke"
        assert s.next_run_at == self._valid_dt

    def test_default_repeat_type_is_once(self):
        s = self._valid()
        assert s.repeat_type == "ONCE"

    def test_default_active_true(self):
        s = self._valid()
        assert s.active is True

    def test_default_execution_mode_docker(self):
        s = self._valid()
        assert s.execution_mode == "docker"

    def test_default_node_ids_empty(self):
        s = self._valid()
        assert s.node_ids == []

    def test_name_min_length_enforced(self):
        with pytest.raises(ValidationError):
            self._valid(name="")

    def test_name_max_length_enforced(self):
        with pytest.raises(ValidationError):
            self._valid(name="x" * 201)

    def test_name_exactly_200_chars_ok(self):
        s = self._valid(name="a" * 200)
        assert len(s.name) == 200

    def test_repeat_type_daily(self):
        s = self._valid(repeat_type="DAILY")
        assert s.repeat_type == "DAILY"

    def test_node_ids_list(self):
        s = self._valid(node_ids=["n1", "n2"])
        assert s.node_ids == ["n1", "n2"]

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            ScheduleCreate(next_run_at=self._valid_dt)

    def test_missing_next_run_at_raises(self):
        with pytest.raises(ValidationError):
            ScheduleCreate(name="Test")


# ── ScheduleUpdate ────────────────────────────────────────────────────


class TestScheduleUpdate:
    def test_all_fields_optional(self):
        u = ScheduleUpdate()
        assert u.name is None
        assert u.repeat_type is None
        assert u.active is None
        assert u.next_run_at is None

    def test_partial_update_name_only(self):
        u = ScheduleUpdate(name="New Name")
        assert u.name == "New Name"
        assert u.repeat_type is None

    def test_name_empty_string_raises(self):
        with pytest.raises(ValidationError):
            ScheduleUpdate(name="")

    def test_active_false_accepted(self):
        u = ScheduleUpdate(active=False)
        assert u.active is False

    def test_execution_mode_local(self):
        u = ScheduleUpdate(execution_mode="local")
        assert u.execution_mode == "local"


# ── ScheduleResponse ──────────────────────────────────────────────────


class TestScheduleResponse:
    _valid_dt = datetime(2026, 6, 1, 9, 0, 0)

    def _valid(self, **overrides):
        defaults = {
            "id": "sched-001",
            "name": "Smoke",
            "node_id": "node-001",
            "project_id": "proj-001",
            "repeat_type": "DAILY",
            "next_run_at": self._valid_dt,
            "active": True,
            "execution_mode": "docker",
            "created_at": self._valid_dt,
            "updated_at": self._valid_dt,
        }
        defaults.update(overrides)
        return ScheduleResponse(**defaults)

    def test_basic_fields_assigned(self):
        r = self._valid()
        assert r.id == "sched-001"
        assert r.name == "Smoke"

    def test_node_ids_defaults_to_empty(self):
        r = self._valid()
        assert r.node_ids == []

    def test_node_titles_defaults_to_empty(self):
        r = self._valid()
        assert r.node_titles == []

    def test_last_run_at_optional_none(self):
        r = self._valid()
        assert r.last_run_at is None

    def test_last_report_id_optional_none(self):
        r = self._valid()
        assert r.last_report_id is None

    def test_node_title_optional(self):
        r = self._valid(node_title="Login Tests")
        assert r.node_title == "Login Tests"

    def test_repeat_config_optional(self):
        r = self._valid(repeat_config="1,3,5")
        assert r.repeat_config == "1,3,5"

    def test_from_attributes_orm_mode(self):
        from pydantic import ConfigDict
        assert ScheduleResponse.model_config.get("from_attributes") is True
