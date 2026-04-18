from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from .models import ChatSession, ChatMessage, UniversityContent
import requests
import json
import uuid


def chat_view(request):
    session_id = request.session.get('chat_session_id')
    
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
        'session_id': session_id
    })


@csrf_exempt
def chat_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Sadece POST istegi kabul edilir.'}, status=405)

    try:
        data = json.loads(request.body)
        question = data.get('question', '').strip()
        session_id = data.get('session_id', '')

        if not question:
            return JsonResponse({'error': 'Lutfen bir soru yazin.'}, status=400)

        try:
            session = ChatSession.objects.get(session_id=session_id)
        except ChatSession.DoesNotExist:
            session = ChatSession.objects.create()

        results = UniversityContent.objects.filter(
            content__icontains=question
        )[:5]

        if not results:
            words = question.split()
            for word in words:
                if len(word) > 3:
                    results = UniversityContent.objects.filter(
                        content__icontains=word
                    )[:5]
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
                json={
                    "model": "mistral",
                    "prompt": prompt,
                    "stream": False
                },
                timeout=60
            )
            response.raise_for_status()
            answer = response.json().get('response', '').strip()

        except requests.exceptions.ConnectionError:
            answer = "Su anda AI servisine ulasilamiyor. Lutfen daha sonra tekrar deneyin."
        except requests.exceptions.Timeout:
            answer = "Cevap uretmek cok uzun surdu. Lutfen tekrar deneyin."
        except Exception as e:
            answer = "Bir hata olustu. Lutfen tekrar deneyin."

        ChatMessage.objects.create(
            session=session,
            question=question,
            answer=answer
        )

        return JsonResponse({
            'answer': answer,
            'session_id': str(session.session_id)
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Gecersiz JSON formati.'}, status=400)
    except Exception as e:
        return JsonResponse({'error': 'Beklenmeyen bir hata olustu.'}, status=500)