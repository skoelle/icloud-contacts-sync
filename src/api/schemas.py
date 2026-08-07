# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
from datetime import date

from pydantic import BaseModel


class ContactOut(BaseModel):
    id: int
    account: str
    uid: str
    full_name: str | None
    prefix: str | None
    given_name: str | None
    middle_name: str | None
    family_name: str | None
    suffix: str | None
    organization: str | None
    job_title: str | None
    birthday: date | None
    notes: str | None
    photo_url: str | None
    emails: list
    phones: list
    addresses: list
    urls: list
    social_profiles: list
    categories: list
    groups: list[str] = []
    updated_at: str

    class Config:
        from_attributes = True


class ContactListResponse(BaseModel):
    total: int
    items: list[ContactOut]


class SyncRunOut(BaseModel):
    id: str
    account: str
    sync_type: str
    started_at: str
    finished_at: str | None
    status: str
    contacts_upserted: int | None
    contacts_deleted: int | None
    error_message: str | None


class GroupMemberOut(BaseModel):
    member_uid: str
    full_name: str | None
    id: int | None = None


class GroupOut(BaseModel):
    id: int
    account: str
    uid: str
    name: str | None
    member_count: int
    updated_at: str

    class Config:
        from_attributes = True


class GroupDetailOut(GroupOut):
    members: list[GroupMemberOut]


class GroupListResponse(BaseModel):
    total: int
    items: list[GroupOut]
