from django.contrib import admin

from parquet_archive.admin import ArchiveAdminMixin
from tests.example.models import ApiRequest, Customer, Payment


class HasBodyFilter(admin.SimpleListFilter):
    title = 'has body'
    parameter_name = 'has_body'

    def lookups(self, request, model_admin):
        return (('yes', 'Yes'),)

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.exclude(body='')
        return None


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    search_fields = ('name',)


@admin.register(ApiRequest)
class ApiRequestAdmin(ArchiveAdminMixin, admin.ModelAdmin):
    list_display = ('id', 'customer', 'method', 'url', 'status_code', 'requested_at')
    list_filter = ('method', 'requested_at', 'customer', 'duration', HasBodyFilter)
    search_fields = ('body', 'url', 'headers', 'customer__name')
    ordering = ('-requested_at',)


@admin.register(Payment)
class PaymentAdmin(ArchiveAdminMixin, admin.ModelAdmin):
    list_display = ('id', 'paid_on', 'description')
    search_fields = ('description',)
    ordering = ('-paid_on',)
