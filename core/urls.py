from django.urls import path
from . import views

urlpatterns = [
    path('upload/', views.DatasetUploadView.as_view(), name='upload'),
    path('chat/<uuid:pk>/', views.ChatView.as_view(), name='chat'),
    path('query/<uuid:session_pk>/', views.query_view, name='query'),
]
