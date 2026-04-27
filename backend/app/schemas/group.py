"""Group / GroupMembership Pydantic Schemas。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class GroupBase(BaseModel):
    name: str
    description: Optional[str] = None
    group_type: str = "team"  # team / squad / dept / project
    parent_id: Optional[str] = None


class GroupCreate(GroupBase):
    # 起始成員清單(可空);建立者會自動成為 owner
    initial_members: list[str] = []


class GroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    group_type: Optional[str] = None
    parent_id: Optional[str] = None


class GroupResponse(GroupBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    organization_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    member_count: int = 0


class GroupMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    group_id: str
    username: str
    role_in_group: str = "member"
    joined_at: datetime
    # 額外欄位(從 User join 來,非 ORM 欄位)
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None


class GroupAddMembersRequest(BaseModel):
    usernames: list[str]
    role_in_group: str = "member"  # owner / admin / member


class GroupMemberUpdateRequest(BaseModel):
    role_in_group: str  # owner / admin / member
