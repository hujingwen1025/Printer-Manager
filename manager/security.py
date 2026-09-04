from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)


def role_for(user):
    if not user.is_authenticated:
        return None
    if user.is_superuser or user.groups.filter(name=ROLE_ADMIN).exists():
        return ROLE_ADMIN
    if user.groups.filter(name=ROLE_OPERATOR).exists():
        return ROLE_OPERATOR
    return ROLE_VIEWER


def has_role(user, *roles):
    return role_for(user) in roles


def roles_required(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not has_role(request.user, *roles):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped
    return decorator


admin_required = roles_required(ROLE_ADMIN)
operator_required = roles_required(ROLE_ADMIN, ROLE_OPERATOR)
