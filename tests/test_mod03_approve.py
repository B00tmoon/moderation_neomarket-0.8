from __future__ import annotations

import uuid

import pytest

from moderation.models import ModerationCard, OutgoingModerationEvent


def make_card(
    *,
    status=ModerationCard.Status.IN_REVIEW,
    moderator_id=None,
    skus=None,
) -> ModerationCard:
    moderator_id = moderator_id or uuid.uuid4()
    return ModerationCard.objects.create(
        product_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        status=status,
        assigned_moderator_id=moderator_id,
        json_after={"skus": skus if skus is not None else [{"id": str(uuid.uuid4())}]},
    )


@pytest.mark.django_db
def test_approve_transitions_to_moderated_and_emits_event(client, settings, monkeypatch) -> None:
    moderator_id = uuid.uuid4()
    card = make_card(moderator_id=moderator_id)
    sent = {}
    settings.B2B_BASE_URL = "https://b2b.example.test"

    class Response:
        status_code = 200

    def fake_post(url, json, headers, timeout):
        sent["url"] = url
        sent["json"] = json
        sent["headers"] = headers
        sent["timeout"] = timeout
        return Response()

    monkeypatch.setattr("moderation.views.requests.post", fake_post)

    response = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {"moderator_comment": "ok"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "MODERATED"
    card.refresh_from_db()
    assert card.status == ModerationCard.Status.MODERATED
    assert card.decision_comment == "ok"
    assert sent["url"] == "https://b2b.example.test/api/v1/moderation/events"
    assert sent["headers"]["X-Service-Key"] == settings.B2B_SERVICE_KEY
    assert sent["json"]["product_id"] == str(card.product_id)
    assert sent["json"]["event_type"] == "MODERATED"
    assert sent["json"]["moderator_id"] == str(moderator_id)
    assert sent["json"]["moderator_comment"] == "ok"
    assert "idempotency_key" in sent["json"]
    assert "occurred_at" in sent["json"]
    assert OutgoingModerationEvent.objects.filter(card=card, event_type="MODERATED", delivered=True).exists()


@pytest.mark.django_db
def test_protocol_approve_returns_ticket_response_shape(client) -> None:
    moderator_id = uuid.uuid4()
    card = make_card(moderator_id=moderator_id)

    response = client.post(
        f"/api/v1/tickets/{card.id}/approve",
        {},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )

    assert response.status_code == 200
    payload = response.json()
    assert {"id", "product_id", "seller_id", "kind", "status", "queue_priority", "created_at"} <= set(payload)
    assert payload["id"] == str(card.id)
    assert payload["status"] == "APPROVED"


@pytest.mark.django_db
def test_approve_others_card_returns_403(client) -> None:
    """Имя теста историческое; по OpenAPI Moderation чужой тикет → 409."""
    card = make_card(moderator_id=uuid.uuid4())

    response = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(uuid.uuid4()),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NOT_ASSIGNED"


@pytest.mark.django_db
def test_approve_after_edited_returns_409(client) -> None:
    moderator_id = uuid.uuid4()
    card = make_card(status=ModerationCard.Status.PENDING, moderator_id=moderator_id)

    response = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_STATUS"


@pytest.mark.django_db
def test_approve_without_sku_returns_409(client) -> None:
    moderator_id = uuid.uuid4()
    card = make_card(moderator_id=moderator_id, skus=[])

    response = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )

    assert response.status_code == 409
    assert response.json()["code"] == "NO_SKUS"


@pytest.mark.django_db
def test_missing_moderator_identity_returns_401(client) -> None:
    card = make_card()

    response = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {},
        content_type="application/json",
    )

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


@pytest.mark.django_db
def test_product_created_creates_pending_card(client, settings) -> None:
    """Канон: PRODUCT_CREATED → карточка в очереди (PENDING)."""
    product_id = uuid.uuid4()
    seller_id = uuid.uuid4()
    settings.SERVICE_KEY = "dev-service-key"

    response = client.post(
        "/api/v1/events/product",
        {
            "event_type": "PRODUCT_CREATED",
            "idempotency_key": str(uuid.uuid4()),
            "occurred_at": "2026-03-15T14:30:00.000Z",
            "payload": {
                "product_id": str(product_id),
                "seller_id": str(seller_id),
                "json_after": {
                    "title": "Test product",
                    "skus": [{"id": str(uuid.uuid4()), "name": "SKU1", "cost_price": 100}],
                },
            },
        },
        content_type="application/json",
        HTTP_X_SERVICE_KEY="dev-service-key",
    )

    assert response.status_code == 202
    card = ModerationCard.objects.get(product_id=product_id)
    assert card.status == ModerationCard.Status.PENDING
    assert card.seller_id == seller_id
    assert card.kind == ModerationCard.Kind.CREATE
    assert card.queue_priority == 1
    # cost_price stripped
    assert "cost_price" not in (card.json_after.get("skus") or [{}])[0]


@pytest.mark.django_db
def test_approve_idempotent_outgoing_event(client, settings, monkeypatch) -> None:
    """Одно исходящее MODERATED на карточку при повторе (стабильный idempotency_key)."""
    moderator_id = uuid.uuid4()
    card = make_card(moderator_id=moderator_id)
    sent_keys = []
    settings.B2B_BASE_URL = "https://b2b.example.test"

    class Response:
        status_code = 200

    def fake_post(url, json, headers, timeout):
        sent_keys.append(json["idempotency_key"])
        return Response()

    monkeypatch.setattr("moderation.views.requests.post", fake_post)

    r1 = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {"moderator_comment": "ok"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert r1.status_code == 200

    # Повтор после успешного решения — не создаёт новое событие
    r2 = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {"moderator_comment": "ok again"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert r2.status_code == 200
    assert OutgoingModerationEvent.objects.filter(card=card, event_type="MODERATED").count() == 1
    assert len(sent_keys) == 1


@pytest.mark.django_db
def test_product_created_claim_approve_end_to_end(client, settings, monkeypatch) -> None:
    """PRODUCT_CREATED → очередь → claim → approve → MODERATED + событие."""
    product_id = uuid.uuid4()
    seller_id = uuid.uuid4()
    moderator_id = uuid.uuid4()
    sent = {}
    settings.B2B_BASE_URL = "https://b2b.example.test"
    settings.SERVICE_KEY = "dev-service-key"

    class Response:
        status_code = 200

    def fake_post(url, json, headers, timeout):
        sent["url"] = url
        sent["json"] = json
        return Response()

    monkeypatch.setattr("moderation.views.requests.post", fake_post)

    created = client.post(
        "/api/v1/events/product",
        {
            "event_type": "PRODUCT_CREATED",
            "idempotency_key": str(uuid.uuid4()),
            "occurred_at": "2026-03-15T14:30:00.000Z",
            "payload": {
                "product_id": str(product_id),
                "seller_id": str(seller_id),
                "json_after": {"title": "Shoes", "skus": [{"id": str(uuid.uuid4())}]},
            },
        },
        content_type="application/json",
        HTTP_X_SERVICE_KEY="dev-service-key",
    )
    assert created.status_code == 202
    card = ModerationCard.objects.get(product_id=product_id)
    assert card.status == ModerationCard.Status.PENDING

    claim = client.post(
        "/api/v1/queue/claim",
        {},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert claim.status_code == 200
    claim_body = claim.json()
    assert claim_body["status"] == "IN_REVIEW"
    assert claim_body["product_id"] == str(product_id)
    assert claim_body["assigned_moderator_id"] == str(moderator_id)

    card.refresh_from_db()
    assert card.status == ModerationCard.Status.IN_REVIEW
    assert card.assigned_moderator_id == moderator_id

    approve = client.post(
        f"/api/v1/products/{product_id}/approve",
        {"moderator_comment": "ok"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "MODERATED"
    card.refresh_from_db()
    assert card.status == ModerationCard.Status.MODERATED
    assert sent["json"]["event_type"] == "MODERATED"
    assert sent["json"]["product_id"] == str(product_id)
    assert OutgoingModerationEvent.objects.filter(card=card, event_type="MODERATED", delivered=True).count() == 1


@pytest.mark.django_db
def test_new_approve_after_edited_emits_new_event(client, settings, monkeypatch) -> None:
    """После EDITED новое одобрение — отдельное событие (не повтор старого ключа)."""
    moderator_id = uuid.uuid4()
    card = make_card(moderator_id=moderator_id)
    sent_keys = []
    settings.B2B_BASE_URL = "https://b2b.example.test"
    settings.SERVICE_KEY = "dev-service-key"

    class Response:
        status_code = 200

    def fake_post(url, json, headers, timeout):
        sent_keys.append(json["idempotency_key"])
        return Response()

    monkeypatch.setattr("moderation.views.requests.post", fake_post)

    r1 = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {"moderator_comment": "first"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert r1.status_code == 200
    assert len(sent_keys) == 1
    first_key = sent_keys[0]

    # PRODUCT_EDITED → снова PENDING
    edited = client.post(
        "/api/v1/b2b/events",
        {
            "event_type": "PRODUCT_EDITED",
            "idempotency_key": str(uuid.uuid4()),
            "occurred_at": "2026-03-16T10:00:00Z",
            "payload": {
                "product_id": str(card.product_id),
                "seller_id": str(card.seller_id),
                "json_before": card.json_after,
                "json_after": {"title": "Updated", "skus": [{"id": str(uuid.uuid4())}]},
            },
        },
        content_type="application/json",
        HTTP_X_SERVICE_KEY="dev-service-key",
    )
    assert edited.status_code == 202
    card.refresh_from_db()
    assert card.status == ModerationCard.Status.PENDING

    claim = client.post(
        "/api/v1/queue/claim",
        {},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert claim.status_code == 200

    r2 = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {"moderator_comment": "second after edit"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert r2.status_code == 200
    assert len(sent_keys) == 2
    assert sent_keys[1] != first_key
    assert OutgoingModerationEvent.objects.filter(card=card, event_type="MODERATED").count() == 2


@pytest.mark.django_db
def test_get_next_canon_path_claims_pending(client) -> None:
    moderator_id = uuid.uuid4()
    card = ModerationCard.objects.create(
        product_id=uuid.uuid4(),
        seller_id=uuid.uuid4(),
        status=ModerationCard.Status.PENDING,
        json_after={"skus": [{"id": str(uuid.uuid4())}]},
    )
    response = client.post(
        "/api/v1/product-moderation/get-next",
        {"queueId": 1},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert response.status_code == 200
    assert response.json()["id"] == str(card.id)
    assert response.json()["status"] == "IN_REVIEW"
    card.refresh_from_db()
    assert card.assigned_moderator_id == moderator_id


@pytest.mark.django_db
def test_new_approve_after_edited_despite_undelivered_previous(client, settings, monkeypatch) -> None:
    """Недоставленное прежнее MODERATED не подменяет новое решение после EDITED."""
    moderator_id = uuid.uuid4()
    card = make_card(moderator_id=moderator_id)
    sent = []
    settings.B2B_BASE_URL = "https://b2b.example.test"
    settings.SERVICE_KEY = "dev-service-key"

    class Ok:
        status_code = 200

    class Fail:
        status_code = 503

    call_count = {"n": 0}

    def fake_post(url, json, headers, timeout):
        call_count["n"] += 1
        # Первая доставка падает — решение уже MODERATED, event undelivered с фиксированным ключом
        if call_count["n"] == 1:
            sent.append({"key": json["idempotency_key"], "comment": json.get("moderator_comment"), "failed": True})
            return Fail()
        sent.append({"key": json["idempotency_key"], "comment": json.get("moderator_comment"), "failed": False})
        return Ok()

    monkeypatch.setattr("moderation.views.requests.post", fake_post)

    # 1) Первое одобрение — B2B недоступен; карточка остаётся MODERATED, outbox undelivered
    r1 = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {"moderator_comment": "first-unconfirmed"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert r1.status_code == 500
    card.refresh_from_db()
    assert card.status == ModerationCard.Status.MODERATED
    assert OutgoingModerationEvent.objects.filter(card=card, event_type="MODERATED", delivered=False).exists()

    old_keys = set(
        str(k)
        for k in OutgoingModerationEvent.objects.filter(card=card, event_type="MODERATED").values_list(
            "idempotency_key", flat=True
        )
    )

    # 2) EDITED → PENDING (новый цикл рассмотрения)
    edited = client.post(
        "/api/v1/b2b/events",
        {
            "event_type": "PRODUCT_EDITED",
            "idempotency_key": str(uuid.uuid4()),
            "occurred_at": "2026-03-16T10:00:00Z",
            "payload": {
                "product_id": str(card.product_id),
                "seller_id": str(card.seller_id),
                "json_before": card.json_after,
                "json_after": {"title": "Updated", "skus": [{"id": str(uuid.uuid4())}]},
            },
        },
        content_type="application/json",
        HTTP_X_SERVICE_KEY="dev-service-key",
    )
    assert edited.status_code == 202
    card.refresh_from_db()
    assert card.status == ModerationCard.Status.PENDING

    claim = client.post(
        "/api/v1/queue/claim",
        {},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert claim.status_code == 200

    # 3) Новое одобрение — обязано уйти с НОВЫМ ключом и комментарием, не со старым undelivered
    r2 = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {"moderator_comment": "second-after-edit"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert r2.status_code == 200
    card.refresh_from_db()
    assert card.status == ModerationCard.Status.MODERATED

    successful = [s for s in sent if not s.get("failed")]
    assert successful, "new decision must be sent to B2B"
    last = successful[-1]
    assert last["comment"] == "second-after-edit"
    assert last["key"] not in old_keys

    delivered_new = OutgoingModerationEvent.objects.filter(
        card=card, event_type="MODERATED", delivered=True
    ).order_by("-created_at")
    assert delivered_new.exists()
    assert str(delivered_new.first().idempotency_key) == last["key"]
    assert delivered_new.first().payload.get("moderator_comment") == "second-after-edit"


@pytest.mark.django_db
def test_approve_redelivery_keeps_same_idempotency_key(client, settings, monkeypatch) -> None:
    """Потеря HTTP-ответа: повтор того же одобрения уходит с тем же idempotency_key."""
    moderator_id = uuid.uuid4()
    card = make_card(moderator_id=moderator_id)
    sent_keys = []
    settings.B2B_BASE_URL = "https://b2b.example.test"

    class Ok:
        status_code = 200

    class Fail:
        status_code = 503

    n = {"c": 0}

    def fake_post(url, json, headers, timeout):
        n["c"] += 1
        sent_keys.append(json["idempotency_key"])
        if n["c"] == 1:
            return Fail()  # B2B мог принять, ответ потерян
        return Ok()

    monkeypatch.setattr("moderation.views.requests.post", fake_post)

    r1 = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {"moderator_comment": "ok"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert r1.status_code == 500
    card.refresh_from_db()
    assert card.status == ModerationCard.Status.MODERATED
    assert len(sent_keys) == 1
    first_key = sent_keys[0]

    # Повтор того же одобрения (клиент не получил 200)
    r2 = client.post(
        f"/api/v1/products/{card.product_id}/approve",
        {"moderator_comment": "ok"},
        content_type="application/json",
        HTTP_X_MODERATOR_ID=str(moderator_id),
    )
    assert r2.status_code == 200
    assert len(sent_keys) == 2
    assert sent_keys[1] == first_key
    assert OutgoingModerationEvent.objects.filter(card=card, event_type="MODERATED").count() == 1
    event = OutgoingModerationEvent.objects.get(card=card, event_type="MODERATED")
    assert str(event.idempotency_key) == first_key
    assert event.delivered is True
