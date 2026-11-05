from django.contrib.auth.models import Group
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class LESocialLoginAdapter(DefaultSocialAccountAdapter):

        def populate_user(self, request, sociallogin, data):
            email = data.get('email', '')
            domain = email.split('@')[-1].lower() if '@' in email else ''
            if domain == "finspot.in":
                return super().populate_user(request, sociallogin, data)

            raise Exception("ERROR INVALID USER")

        def save_user(self, request, sociallogin, form=None):
            user = super().save_user(request, sociallogin, form)
            legroup = Group.objects.get(name='Google')
            user.groups.add(legroup)
            return user

        def is_auto_signup_allowed(self, request, sociallogin):
            return True