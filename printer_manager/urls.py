from django.contrib.auth import views as auth_views
from django.urls import include, path

from manager.forms import LoginForm


urlpatterns = [
    path("healthz", include("manager.health_urls")),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html", authentication_form=LoginForm), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("manager.urls")),
]
