import uuid

from django.db import models
from django.utils import timezone


class UppercaseField(models.CharField):
    """Field with from_db_value, like encrypted fields and custom value objects."""

    def from_db_value(self, value, expression, connection):
        return value.upper() if value else value


class OnDeleteGuard:
    """Wraps on_delete like django-polymorphic's PolymorphicGuard."""

    def __init__(self, action):
        self.action = action

    def __call__(self, *args, **kwargs):
        return self.action(*args, **kwargs)


class Customer(models.Model):
    name = models.CharField(max_length=100)
    registered_at = models.DateField(null=True, blank=True)

    def __str__(self):
        return self.name


class ApiRequest(models.Model):
    class Method(models.TextChoices):
        GET = 'get'
        POST = 'post'
        PUT = 'put'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    requested_at = models.DateTimeField(default=timezone.now)
    url = models.URLField()
    method = models.CharField(max_length=10, choices=Method.choices)
    body = models.TextField(blank=True)
    status_code = models.IntegerField(null=True, blank=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    success = models.BooleanField(default=False)
    reference_date = models.DateField(null=True, blank=True)
    headers = models.JSONField(default=dict, blank=True)
    duration = models.DurationField(null=True, blank=True)
    external_code = UppercaseField(max_length=20, blank=True)
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, null=True, blank=True, related_name='requests'
    )
    retry_of = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='retries'
    )


class Tag(models.Model):
    name = models.CharField(max_length=50)


class Order(models.Model):
    created_at = models.DateTimeField(default=timezone.now)
    tags = models.ManyToManyField(Tag, blank=True)


class Attachment(models.Model):
    order = models.ForeignKey(Order, on_delete=OnDeleteGuard(models.CASCADE), related_name='attachments')
