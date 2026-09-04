import json

from django import template


register = template.Library()


@register.filter
def pretty_json(value):
    return json.dumps(value or {}, indent=2, sort_keys=True, ensure_ascii=False, default=str)
