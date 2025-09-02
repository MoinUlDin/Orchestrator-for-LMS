from django.urls import path
from .views import provision_request_view, executeme

urlpatterns = [
    path("provision/", provision_request_view, name="provision"),
    path("execute_fun/", executeme, name="execute_fun"),
]