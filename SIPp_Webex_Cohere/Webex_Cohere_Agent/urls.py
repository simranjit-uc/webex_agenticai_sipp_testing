from django.urls import path
from . import views, views_msteams

urlpatterns = [
    path("chat_webhook/", views.chat_webhook, name="chat_webhook"),
    path("webhook/teams/", views_msteams.chat_webhook_teams),
]
