from django.core.exceptions import PermissionDenied
from django.http import HttpResponse

def role_required(allowed_roles=[]):
    def decorator(view_func):
        def wrap(request, *args, **kwargs):
            # Highest-weightage group, matching how useronboard's get_userlist
            # resolves the user's displayed role. Was groups.first(), which has
            # no ORDER BY -- so for a user in more than one group the enforced
            # role could differ from the one the admin page showed, and wasn't
            # even stable between queries.
            top_group = request.user.groups.order_by('-weightage').first()
            user_group = top_group.name if top_group else None
            if user_group in allowed_roles:
                return view_func(request, *args, **kwargs)
            return HttpResponse("You are not authorized to view this page", status=403)
        return wrap
    return decorator