from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from .models import ChatSession, ChatMessage, UniversityContent, UserProfile
import requests
import json


def login_view(request):
    if request.session.get('user_id'):
        return redirect('chat')
    return render(request, 'chat/login.html')


@csrf_exempt
def login_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Sadece POST isteği kabul edilir.'}, status=405)
    try:
        data = json.loads(request.body)
        user_type = data.get('user_type')

        if user_type == 'student':
            student_number = data.get('student_number', '').strip()
            password = data.get('password', '').strip()
            if not student_number:
                return JsonResponse({'error': 'Öğrenci numarası gereklidir.'}, status=400)
            user, created = UserProfile.objects.get_or_create(
                student_number=student_number,
                defaults={
                    'name': f'Öğrenci {student_number}',
                    'user_type': 'student'
                }
            )
            request.session['user_id'] = user.id
            request.session['user_name'] = user.name
            request.session['user_type'] = 'student'
            return JsonResponse({
                'success': True,
                'user_name': user.name,
                'user_type': 'student',
                'redirect': '/'
            })

        elif user_type == 'guest':
            name = data.get('name', '').strip()
            email = data.get('email', '').strip()
            if not name:
                return JsonResponse({'error': 'Ad soyad gereklidir.'}, status=400)
            user = UserProfile.objects.create(
                name=name,
                email=email if email else None,
                user_type='guest'
            )
            request.session['user_id'] = user.id
            request.session['user_name'] = user.name
            request.session['user_type'] = 'guest'
            return JsonResponse({
                'success': True,
                'user_name': user.name,
                'user_type': 'guest',
                'redirect': '/'
            })

        return JsonResponse({'error': 'Geçersiz kullanıcı tipi.'}, status=400)

    except Exception as e:
        return JsonResponse({'error': 'Bir hata oluştu.'}, status=500)


def logout_view(request):
    request.session.flush()
    return redirect('login')


def chat_view(request):
    if not request.session.get('user_id'):
        return redirect('login')

    session_id = request.session.get('chat_session_id')
    user_id = request.session.get('user_id')
    user_name = request.session.get('user_name', 'Kullanıcı')
    user_type = request.session.get('user_type', 'guest')

    if not session_id:
        session = ChatSession.objects.create()
        request.session['chat_session_id'] = str(session.session_id)
        session_id = str(session.session_id)

    try:
        session = ChatSession.objects.get(session_id=session_id)
        messages = session.messages.all()
    except ChatSession.DoesNotExist:
        session = ChatSession.objects.create()
        request.session['chat_session_id'] = str(session.session_id)
        messages = []

    return render(request, 'chat/index.html', {
        'messages': messages,
        'session_id': session_id,
        'user_name': user_name,
        'user_type': user_type,
    })


@csrf_exempt
def chat_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Sadece POST isteği kabul edilir.'}, status=405)
    try:
        data = json.loads(request.body)
        question = data.get('question', '').strip()
        session_id = data.get('session_id', '')
        if not question:
            return JsonResponse({'error': 'Lütfen bir soru yazın.'}, status=400)
        try:
            session = ChatSession.objects.get(session_id=session_id)
        except ChatSession.DoesNotExist:
            session = ChatSession.objects.create()

        results = UniversityContent.objects.filter(content__icontains=question)[:5]
        if not results:
            words = question.split()
            for word in words:
                if len(word) > 3:
                    results = UniversityContent.objects.filter(content__icontains=word)[:5]
                    if results:
                        break

        if results:
            context = "\n\n".join([
                "Baslik: " + r.title + "\nIcerik: " + r.content[:500]
                for r in results
            ])
        else:
            context = "Bu konuda veritabaninda bilgi bulunamadi."

        prompt = (
            "Sen Acibadem Universitesi hakkinda sorulara cevap veren yardimci bir asistansin.\n"
            "Asagidaki bilgileri kullanarak soruyu Turkce olarak cevapla.\n"
            "Eger bilgi yetersizse 'Bu konuda yeterli bilgim bulunmamaktadir' de.\n"
            "Net ve anlasilir ol.\n\n"
            "Bilgi Tabani:\n" + context + "\n\n"
            "Soru: " + question + "\n\n"
            "Cevap:"
        )

        try:
            response = requests.post(
                settings.OLLAMA_URL + "/api/generate",
                json={"model": "mistral", "prompt": prompt, "stream": False},
                timeout=60
            )
            response.raise_for_status()
            answer = response.json().get('response', '').strip()
        except requests.exceptions.ConnectionError:
            answer = "Su anda AI servisine ulasilamiyor. Lutfen daha sonra tekrar deneyin."
        except requests.exceptions.Timeout:
            answer = "Cevap uretmek cok uzun surdu. Lutfen tekrar deneyin."
        except Exception:
            answer = "Bir hata olustu. Lutfen tekrar deneyin."

        ChatMessage.objects.create(
            session=session,
            question=question,
            answer=answer
        )

        return JsonResponse({'answer': answer, 'session_id': str(session.session_id)})

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Gecersiz JSON formati.'}, status=400)
    except Exception:
        return JsonResponse({'error': 'Beklenmeyen bir hata olustu.'}, status=500)


@csrf_exempt
def search_api(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'Sadece GET isteği kabul edilir.'}, status=405)
    query = request.GET.get('q', '').strip()
    if not query:
        return JsonResponse({'results': []})
    results = UniversityContent.objects.filter(content__icontains=query)[:8]
    if not results:
        words = query.split()
        for word in words:
            if len(word) > 3:
                results = UniversityContent.objects.filter(content__icontains=word)[:8]
                if results:
                    break
    data = [
        {'title': r.title, 'snippet': r.content[:200], 'url': r.url, 'category': r.category}
        for r in results
    ]
    return JsonResponse({'results': data, 'query': query})


@csrf_exempt
def feedback_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Sadece POST isteği kabul edilir.'}, status=405)
    try:
        data = json.loads(request.body)
        return JsonResponse({'status': 'ok', 'helpful': data.get('helpful')})
    except Exception:
        return JsonResponse({'error': 'Hata olustu.'}, status=500)