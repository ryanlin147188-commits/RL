from .base import Base
from .defect import Defect, DefectPriority, DefectSeverity, DefectStatus
from .execution_report import ExecutionReport, ReportStatus
from .execution_step_log import ExecutionStepLog, StepStatus
from .project import Project
from .recording import RecordingSession
from .requirement import (
    Requirement,
    RequirementPriority,
    RequirementSource,
    RequirementStatus,
    RequirementTestcaseLink,
)
from .audit_log import AuditLog
from .email_config import EmailConfig
from .entity_version import EntityVersion
from .group import Group, GroupMembership
from .mock_endpoint import MockEndpoint
from .notification import Notification
from .notification_preference import NotificationPreference
from .oidc_provider import OidcProvider
from .org_invite import OrgInvite
from .org_membership import OrgMembership
from .organization import Organization
from .password_reset_token import PasswordResetToken
from .project_member import ProjectMember
from .role import Role
from .user import User
from .project_env_var import ProjectEnvVar
from .review import (
    ReviewableEntityType,
    ReviewAction,
    ReviewHistory,
    ReviewRecord,
    ReviewStatus,
)
from .schedule import RepeatType, Schedule
from .step_screenshot_baseline import StepScreenshotBaseline
from .test_data_set import DataSetCategory, TestDataSet
from .test_milestone import MilestoneStatus, TestMilestone
from .test_plan import TestPlan, TestPlanStatus
from .test_round import TestRound
from .test_version import TestVersion, VersionPlatform, VersionStatus
from .testcase_content import TestcaseContent
from .testcase_env_binding import TestcaseEnvBinding
from .testcase_precondition_link import TestcasePreconditionLink
from .todo_item import TodoItem, TodoItemType, TodoPriority, TodoStatus
from .todo_link import ALLOWED_TARGET_TYPES, TodoLink
from .tree_node import LevelType, TreeNode
from .wbs_item import WbsItem, WbsItemType, WbsStatus
from .wbs_link import ALLOWED_WBS_TARGET_TYPES, WbsLink

__all__ = [
    "Base",
    "Project",
    "TreeNode",
    "LevelType",
    "TestcaseContent",
    "TestcasePreconditionLink",
    "TestcaseEnvBinding",
    "ExecutionReport",
    "ReportStatus",
    "ExecutionStepLog",
    "StepStatus",
    "RecordingSession",
    # Test management extensions (defect / milestone / plan / requirement / RTM)
    "Defect", "DefectSeverity", "DefectPriority", "DefectStatus",
    "TestMilestone", "MilestoneStatus",
    "TestPlan", "TestPlanStatus",
    "TestVersion", "VersionPlatform", "VersionStatus",
    "Requirement", "RequirementSource", "RequirementPriority",
    "RequirementStatus", "RequirementTestcaseLink",
    "TestDataSet", "DataSetCategory",
    "WbsItem", "WbsItemType", "WbsStatus",
    "WbsLink", "ALLOWED_WBS_TARGET_TYPES",
    # Settings + todos
    "Role", "NotificationPreference", "Notification", "EmailConfig",
    "TodoItem", "TodoItemType", "TodoStatus", "TodoPriority",
    "TodoLink", "ALLOWED_TARGET_TYPES",
    "User",
    # Multi-tenancy + audit
    "Organization", "AuditLog", "OrgInvite", "OrgMembership",
    # Forgot-password flow
    "PasswordResetToken",
    # Generic entity versioning (AI draft / pending review / approved / rejected + revert)
    "EntityVersion",
    # Per-project membership + roles
    "ProjectMember",
    # SSO / OIDC
    "OidcProvider",
    # Mock + DB connection persistence (取代 localStorage)
    "MockEndpoint",
    # Groups (團隊群組,可巢狀,可作為 todo assignee)
    "Group", "GroupMembership",
    # Project-level config + execution artefacts
    "ProjectEnvVar",
    "Schedule", "RepeatType",
    "StepScreenshotBaseline",
    "TestRound",
    # Review / approval workflow
    "ReviewRecord", "ReviewHistory",
    "ReviewableEntityType", "ReviewStatus", "ReviewAction",
]
