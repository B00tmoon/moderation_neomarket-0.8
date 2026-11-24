from __future__ import annotations

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError

from .models import BlockingReason, IncomingProductEvent, ModerationCard, OutgoingModerationEvent


@admin.register(BlockingReason)
class BlockingReasonAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "hard_block", "is_active")
    list_filter = ("hard_block", "is_active")
    search_fields = ("code", "title")
    readonly_fields = ("created_at", "updated_at")
    actions = ("deactivate",)

    @admin.action(description="Deactivate selected reasons")
    def deactivate(self, request, queryset):
        queryset.update(is_active=False)

    def delete_model(self, request, obj):
        obj.delete()

    def delete_queryset(self, request, queryset):
        queryset.update(is_active=False)


@admin.register(ModerationCard)
class ModerationCardAdmin(admin.ModelAdmin):
    list_display = ("product_id", "status", "assigned_moderator_id", "blocking_reason", "decision_at")
    list_filter = ("status", "blocking_reason", "kind")
    search_fields = ("product_id", "seller_id", "assigned_moderator_id")
    readonly_fields = ("created_at", "updated_at", "claimed_at", "claim_expires_at")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        # Обычные админы не меняют терминальный HARD_BLOCKED
        if obj is not None and obj.status == ModerationCard.Status.HARD_BLOCKED and not request.user.is_superuser:
            readonly = list(set(readonly) | {"status", "blocking_reason", "decision_at", "product_id"})
        return readonly

    def save_model(self, request, obj, form, change):
        if change and "status" in form.changed_data:
            try:
                original = ModerationCard.objects.get(pk=obj.pk)
            except ModerationCard.DoesNotExist:
                original = None
            if original is not None and original.status == ModerationCard.Status.HARD_BLOCKED:
                if original.status != obj.status:
                    # Аварийное снятие HARD_BLOCKED — только суперадмин + причина
                    if not request.user.is_superuser:
                        raise PermissionDenied(
                            "Only a superuser may lift HARD_BLOCKED status outside the normal flow."
                        )
                    reason = (obj.decision_comment or "").strip()
                    if not reason:
                        raise ValidationError(
                            {
                                "decision_comment": (
                                    "Emergency unlock of HARD_BLOCKED requires a non-empty "
                                    "decision_comment stating the reason."
                                )
                            }
                        )
                    messages.warning(
                        request,
                        f"HARD_BLOCKED lifted by superuser {request.user}: {reason}",
                    )
        super().save_model(request, obj, form, change)


@admin.register(OutgoingModerationEvent)
class OutgoingModerationEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "card", "delivered", "created_at", "delivered_at")
    list_filter = ("event_type", "delivered")
    search_fields = ("idempotency_key", "card__product_id")


@admin.register(IncomingProductEvent)
class IncomingProductEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "product_id", "idempotency_key", "created_at")
    list_filter = ("event_type",)
    search_fields = ("idempotency_key", "product_id")
