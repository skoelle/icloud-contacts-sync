# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
from datetime import date
from pydantic import BaseModel


class ContactOut(BaseModel):
    id: int
    account: str
    uid: str
    full_name: str | None
    given_name: str | None
    family_name: str | None
    organization: str | None
    job_title: str | None
    birthday: date | None
    notes: str | None
    emails: list
    phones: list
    addresses: list
    urls: list
    social_profiles: list
    categories: list
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
