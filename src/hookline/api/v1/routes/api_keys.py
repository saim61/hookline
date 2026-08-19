from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from hookline.api.deps import ApiKeyRepoDep
from hookline.auth.dependencies import requires
from hookline.auth.scopes import Scope
from hookline.schemas.api_key import ApiKeyCreate, ApiKeyCreated, ApiKeyRead

# Every route here needs admin: a key able to mint keys can grant itself anything, so
# there is no meaningful narrower scope for it.
router = APIRouter(
    prefix="/api-keys",
    tags=["api-keys"],
    dependencies=[requires(Scope.ADMIN)],
)


@router.post("", response_model=ApiKeyCreated, status_code=status.HTTP_201_CREATED)
async def create_api_key(payload: ApiKeyCreate, repo: ApiKeyRepoDep) -> ApiKeyCreated:
    """Mint a key. The `key` field is returned exactly once and cannot be recovered."""
    api_key, token = await repo.create(
        name=payload.name,
        scopes=payload.scopes,
        expires_at=payload.expires_at,
        with_inbound_signing=payload.require_signed_requests,
    )
    return ApiKeyCreated(
        **ApiKeyRead.model_validate(api_key).model_dump(),
        key=token,
        inbound_signing_secret=api_key.inbound_signing_secret,
    )


@router.get("", response_model=list[ApiKeyRead])
async def list_api_keys(repo: ApiKeyRepoDep) -> list[ApiKeyRead]:
    return [ApiKeyRead.model_validate(k) for k in await repo.list_all()]


@router.get("/{key_id}", response_model=ApiKeyRead)
async def get_api_key(key_id: UUID, repo: ApiKeyRepoDep) -> ApiKeyRead:
    api_key = await repo.get(key_id)
    if api_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="api key not found")
    return ApiKeyRead.model_validate(api_key)


@router.post("/{key_id}/revoke", response_model=ApiKeyRead)
async def revoke_api_key(key_id: UUID, repo: ApiKeyRepoDep) -> ApiKeyRead:
    """Deactivate a key, keeping the row for audit.

    Revoke rather than delete so `name` and `last_used_at` survive - during an incident,
    "what was this key doing before we killed it" is exactly the question being asked.
    """
    if not await repo.revoke(key_id):
        existing = await repo.get(key_id)
        if existing is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="api key not found")
        raise HTTPException(status.HTTP_409_CONFLICT, detail="api key is already revoked")

    revoked = await repo.get(key_id)
    assert revoked is not None
    return ApiKeyRead.model_validate(revoked)
