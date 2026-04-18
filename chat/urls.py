from django.urls import path
from . import views

urlpatterns = [
    path('', views.chat_view, name='chat'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('api/login/', views.login_api, name='login_api'),
    path('api/chat/', views.chat_api, name='chat_api'),
    path('api/search/', views.search_api, name='search_api'),
    path('api/feedback/', views.feedback_api, name='feedback_api'),
]