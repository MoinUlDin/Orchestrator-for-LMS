from django.urls import path
from .views import ProvisionView

urlpatterns = [
    path("provision/", ProvisionView.as_view(), name="provision"),
]