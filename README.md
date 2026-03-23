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

## 🚀 Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/furkandalyan/ACU-CHATBOT.git
cd acibadem-chatbot
```

### 2. Configure Environment Variables

Create a `.env` file based on `.env.example` and update the required values such as database credentials.

---

### 3. Start the Application

```bash
docker-compose up --build
```

---

### 4. Access the Application

Once the containers are running:

* Web Application: http://localhost:8000
* API Endpoint: http://localhost:8000/api/chat/

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
