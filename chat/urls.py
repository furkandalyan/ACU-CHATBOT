from django.urls import path
from . import views

urlpatterns = [
    path('', views.chat_view, name='chat'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('api/login/', views.login_api, name='login_api'),
    path('api/chat/', views.chat_api, name='chat_api'),
    path('api/chat/stream/', views.chat_stream_api, name='chat_stream_api'),
    path('api/search/', views.search_api, name='search_api'),
    path('api/feedback/', views.feedback_api, name='feedback_api'),
    path('api/session/new/', views.new_session_api, name='new_session_api'),
    path('api/session/clear/', views.clear_sessions_api, name='clear_sessions_api'),
    path('api/session/<uuid:session_id>/delete/', views.delete_session_api, name='delete_session_api'),
]
