from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from login.decorators import role_required


# Create your views here.
@login_required(login_url="/")
def userprofile(request):
	return render(request, 'app/profile.html')