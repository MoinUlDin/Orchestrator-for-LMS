from django.urls import path
from .views import provision_request_view

urlpatterns = [
    path("provision/", provision_request_view, name="provision"),
]