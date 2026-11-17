# SPDX-FileCopyrightText: 2026 Alberto Borsetta
# SPDX-License-Identifier: AGPL-3.0-or-later
from fastapi import APIRouter

router = APIRouter()


@router.get('/health')
def healthcheck():
    return {'status': 'ok'}
