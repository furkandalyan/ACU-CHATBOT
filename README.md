# ACU AI Chatbot

## 📌 Project Overview

The ACU AI Chatbot is a web-based application designed to answer questions about Acıbadem University using artificial intelligence. The system utilizes real data collected from the university’s official websites and generates responses using a locally running Large Language Model (LLM).

The project is developed as part of the CSE 322 – Cloud Computing course and focuses on containerization, system architecture, and AI integration.

---

## 🎯 Objectives

* Provide accurate answers about Acıbadem University
* Use real data from official university sources
* Run a fully local AI model (no external APIs)
* Build a scalable and containerized system using Docker

---

## 🏗️ System Architecture

The system consists of three main components:

1. **Web Application (Django)**
   Handles user interactions and processes requests.

2. **Database (PostgreSQL)**
   Stores collected university data and chat history.

3. **LLM Service (Ollama + Mistral)**
   Generates responses based on user queries and context.

### 🔄 Workflow

1. User submits a question via the web interface
2. Django processes the request
3. Relevant data is retrieved from the database
4. Context + question is sent to the LLM
5. LLM generates a response
6. The response is returned to the user

---

## ⚙️ Technologies Used

* **Backend:** Django
* **Database:** PostgreSQL
* **AI Model:** Mistral (via Ollama)
* **Containerization:** Docker & Docker Compose
* **Data Collection:** BeautifulSoup / Selenium

---

## 🚀 Docker Setup

### 1. Clone the Repository

```bash
git clone https://github.com/mahirfurkandalyan/ACU-CHATBOT.git
cd ACU-CHATBOT
```

### 2. Configure Environment Variables

The project runs with safe demo defaults, but you can create a local `.env` file to override them:

```bash
cp .env.example .env
```

---

### 3. Start the Application

```bash
docker compose up --build
```

This single command starts:

* `web` - Django application and REST API
* `postgres` - PostgreSQL 15 database for university content, chat history, and app data
* `ollama` - local open-source LLM server
* `ollama-pull` - one-time helper that downloads the configured Ollama model

On first startup, downloading the LLM model can take several minutes. The default model is `qwen2.5:3b`; change `OLLAMA_MODEL` in `.env` if your computer needs a different model.

---

### 4. Access the Application

Once the containers are running:

* Web Application: http://localhost:8000
* API Endpoint: http://localhost:8000/api/chat/
* Django Admin: http://localhost:8000/admin/

Demo admin:

```text
username: admin
password: admin123
```

Demo student numbers:

```text
221401001
221401002
221401003
221401004
```

The current demo login flow accepts any non-empty password for seeded student users.

### 5. Load University Data

The database schema and demo users are prepared automatically by the web container. To collect public Acıbadem University content for the chatbot knowledge base, run:

```bash
docker compose exec web python scraper/run_all.py
```

For a lighter static scrape:

```bash
docker compose exec web python scraper/run_all.py --only bs4
```

The scraper only targets publicly available pages and includes delays in the scraping code to avoid aggressive requests.

### Useful Docker Commands

```bash
docker compose ps
docker compose logs -f web
docker compose logs -f ollama
docker compose down
docker compose down -v
```

---

## 🤖 AI Integration

The chatbot uses a locally running LLM (Mistral) served via Ollama. The model is integrated using HTTP API requests. Prompt engineering techniques are applied to ensure that the responses are accurate and based on real university data.

---

## 📊 Evaluation Plan

The system will be evaluated using a set of sample questions related to:

* Academic programs
* Course descriptions
* Admission processes
* Campus facilities

The responses will be analyzed based on accuracy, relevance, and clarity.

---

## ⚠️ Challenges

* Integrating the LLM with Django
* Ensuring accurate and context-based responses
* Managing container communication
* Handling performance constraints in local environments

---

## 👥 Team Members

* **Mahir** – AI & LLM Integration
* **Eylül** – Backend Development (Django)
* **Buğra** – Database & Data Collection
* **Sevde** – DevOps & Docker

---

## 📌 Notes

* This project uses only locally running AI models (no external APIs).
* Data is collected only from publicly available university sources.
* The system is designed to be modular and scalable.

---

## 📄 License

This project is developed for educational purposes as part of the CSE 322 course.
