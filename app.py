import os
from dotenv import load_dotenv
load_dotenv()

import re
import tempfile
import requests
import uuid
import threading
from flask import Flask, request, render_template, redirect, url_for, send_file, session
from fpdf import FPDF
from openai import OpenAI
import stripe

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# ------------------------------
# 1) CONFIG : OpenAI, Stripe, Lulu API
# ------------------------------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
stripe.api_key = os.getenv("STRIPE_API_KEY")
stripe_public_key = os.getenv("STRIPE_PUBLIC_KEY")

LULU_CLIENT_KEY = os.getenv("LULU_CLIENT_KEY")
LULU_CLIENT_SECRET = os.getenv("LULU_CLIENT_SECRET")
LULU_AUTH_HEADER = os.getenv("LULU_AUTH_HEADER")

# Prix par défaut du produit (en centimes)
PRICE = 1000

# -----------------------------
# 2) ROUTE D'ACCUEIL
# -----------------------------
@app.route("/")
def index():
    # Chargez votre template index.html (avec le formulaire)
    return render_template("index.html", stripe_public_key=stripe_public_key)

# -----------------------------
# 2bis) NOUVELLE ROUTE : CHOISIR LE PLAN
# -----------------------------
@app.route("/choose-plan", methods=["POST"])
def choose_plan():
    """
    Récupère les infos initiales du formulaire de index.html et les stocke en session,
    puis affiche la page de sélection de plan (choose_plan.html).
    """
    data = request.form.to_dict()
    session["order_data"] = data  # Stocke les infos personnelles et préférences
    return render_template("choose_plan.html")

# -----------------------------
# 3) CREATION DE SESSION STRIPE & REDIRECTION
# -----------------------------
@app.route("/prepare-payment", methods=["POST"])
def prepare_payment():
    """
    Récupère les informations du plan choisi, crée la session Stripe et redirige l'utilisateur.
    """
    # Si le formulaire contient le champ "plan", c'est issu de choose_plan.html
    if "plan" in request.form:
        plan = request.form["plan"]
        session["plan"] = plan
        # Récupère les données stockées lors du remplissage initial
        data = session.get("order_data", {})
        data["plan"] = plan
    else:
        data = request.form.to_dict()
        plan = data.get("plan", "")
    
    # Vérifier que tous les champs requis sont présents
    required_fields = ["name", "age", "appearance", "style", "ambiance", "theme", "email", "plan"]
    if not all(field in data and data[field] for field in required_fields):
        return "Erreur: champs requis manquants", 400

    # Génère un order_id unique et stocke les infos en session
    order_id = str(uuid.uuid4())
    session["order_data"] = data
    session["order_id"] = order_id

    # Détermine le prix selon le plan choisi
    if plan == "ebook":
        unit_amount = 999  # ex: 9.99 €
    elif plan == "livre_physique":
        unit_amount = 1999  # 19.99 €
    elif plan == "bundle":
        unit_amount = 2499  # 24.99 €
    else:
        unit_amount = PRICE

    try:
        # Configuration de la collecte d'adresse pour les plans physiques
        shipping_params = {}
        if plan in ["livre_physique", "bundle"]:
            shipping_params = {
                "shipping_address_collection": {
                    "allowed_countries": ["FR", "BE", "US", "CA"]
                },
                "shipping_options": [
                    {
                        "shipping_rate_data": {
                            "display_name": "Livraison standard",
                            "type": "fixed_amount",
                            "fixed_amount": {
                                "amount": 0,
                                "currency": "eur"
                            },
                            "delivery_estimate": {
                                "minimum": {"unit": "business_day", "value": 5},
                                "maximum": {"unit": "business_day", "value": 10}
                            }
                        }
                    }
                ]
            }

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "product_data": {
                        "name": "BD Personnalisée",
                        "description": f"BD pour {data['name']} ({plan})"
                    },
                    "unit_amount": unit_amount,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url=url_for("payment_success", _external=True) + f"?session_id={{CHECKOUT_SESSION_ID}}&order_id={order_id}",
            cancel_url=url_for("payment_cancel", _external=True),
            client_reference_id=order_id,
            customer_email=data["email"],
            **shipping_params
        )

        # Redirection automatique vers la page Stripe
        return redirect(checkout_session.url, code=303)

    except Exception as e:
        return f"Erreur Stripe: {str(e)}", 500

# -----------------------------
# 4) TRAITEMENT DES COMMANDES
# -----------------------------
def process_comic_order(order_data, email, plan):
    """
    Fonction pour générer la BD.
    """
    try:
        # Générer la BD (PDF)
        pdf_path = generate_comic_book(order_data)
        if pdf_path:
            # Stocker le chemin dans la session
            session["pdf_path"] = pdf_path
            
            # Si livre physique, envoyer à Lulu
            if plan in ["livre_physique", "bundle"]:
                send_to_lulu(pdf_path, order_data)
                
            return pdf_path
    except Exception as e:
        print(f"Erreur lors du traitement de la commande: {str(e)}")
        return None

def send_to_lulu(pdf_path, order_data):
    """
    Envoie le PDF à l'API de Lulu pour l'impression et la livraison.
    """
    try:
        LULU_API_ENDPOINT = "https://api.lulu.com/print-jobs/"
        
        # Préparation des données pour Lulu
        with open(pdf_path, "rb") as pdf_file:
            # Création d'un print job avec l'authentification correcte
            headers = {
                "Authorization": LULU_AUTH_HEADER,
                "Content-Type": "application/json"
            }
            
            # Données du livre
            payload = {
                "line_items": [{
                    "title": f"BD Personnalisée - {order_data['name']}",
                    "quantity": 1,
                    "cover_finish": "matte",
                    "interior_color": "color",
                    "interior_paper": "standard",
                    "binding": "perfect",
                    "size": "US_LETTER",
                    "page_count": 12  # Ajustez selon votre BD (10 planches + couverture)
                }],
                "shipping_address": {
                    "name": order_data["name"],
                    "street1": order_data.get("address", ""),
                    "city": order_data.get("city", ""),
                    "country_code": order_data.get("country", "FR"),
                    "postcode": order_data.get("postcode", "")
                },
                "contact_email": order_data["email"]
            }
            
            # Création du print job
            response = requests.post(
                LULU_API_ENDPOINT, 
                json=payload,
                headers=headers
            )
            
            if response.status_code == 201:
                print_job_id = response.json()["id"]
                
                # Upload du PDF
                upload_url = f"{LULU_API_ENDPOINT}{print_job_id}/file"
                files = {"file": (f"bd_{order_data['name']}.pdf", pdf_file)}
                upload_response = requests.post(upload_url, files=files, headers=headers)
                
                if upload_response.status_code == 200:
                    # Lancement de la production
                    production_url = f"{LULU_API_ENDPOINT}{print_job_id}/production"
                    prod_response = requests.post(production_url, headers=headers)
                    
                    if prod_response.status_code == 200:
                        print(f"Commande Lulu créée avec succès: {print_job_id}")
                        return True
            
            print(f"Erreur API Lulu: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"Erreur lors de l'appel à l'API de Lulu: {str(e)}")
        return False

# -----------------------------
# 5) ROUTE SUCCÈS APRES PAIEMENT
# -----------------------------
@app.route("/payment-success")
def payment_success():
    """
    Vérifie que le paiement est validé et lance la génération de la BD.
    """
    session_id = request.args.get("session_id")
    order_id = request.args.get("order_id")

    # Vérifier cohérence
    if order_id != session.get("order_id"):
        return redirect(url_for("index"))

    order_data = session.get("order_data", {})
    if not order_data:
        return redirect(url_for("index"))

    try:
        # Vérifier dans Stripe
        stripe_session = stripe.checkout.Session.retrieve(session_id)
        if stripe_session.payment_status == "paid":
            email = stripe_session.customer_details.email
            plan = session.get("plan", "ebook")

            # Récupérer l'adresse de livraison si plan = livre_physique ou bundle
            if plan in ["livre_physique", "bundle"]:
                shipping_info = stripe_session.shipping
                if shipping_info and shipping_info.address:
                    order_data["address"] = shipping_info.address.line1 or ""
                    order_data["city"] = shipping_info.address.city or ""
                    order_data["postcode"] = shipping_info.address.postal_code or ""
                    order_data["country"] = shipping_info.address.country or "FR"

            # Pour afficher immédiatement la page de traitement
            return render_template("processing.html", email=email, plan=plan)
        else:
            # Paiement non validé
            return redirect(url_for("index"))

    except Exception as e:
        return f"Erreur: {str(e)}", 500

    return redirect(url_for("index"))

# -----------------------------
# 6) ROUTE DE TRAITEMENT ET GÉNÉRATION
# -----------------------------
@app.route("/process-comic")
def process_comic():
    """
    Route appelée par AJAX depuis la page processing.html
    pour lancer la génération du comic et retourner le pdf_path
    """
    order_data = session.get("order_data", {})
    plan = session.get("plan", "ebook")
    email = order_data.get("email", "")
    
    if not order_data:
        return {"success": False, "error": "Données de commande non trouvées"}, 400
    
    # Générer le comic de manière synchrone (puisque l'utilisateur attend)
    pdf_path = process_comic_order(order_data, email, plan)
    
    if pdf_path:
        # Stocker le chemin dans la session
        session["pdf_path"] = pdf_path
        return {"success": True, "plan": plan}
    else:
        return {"success": False, "error": "Échec de la génération de la BD"}, 500

# -----------------------------
# 7) ROUTE POUR AFFICHER LE PDF
# -----------------------------
@app.route("/view-pdf")
def view_pdf():
    """
    Affiche directement le PDF dans le navigateur
    """
    pdf_path = session.get("pdf_path")
    if not pdf_path or not os.path.exists(pdf_path):
        return "PDF non disponible", 404
    
    # Envoyer le fichier avec disposition 'inline' pour qu'il s'ouvre dans le navigateur
    return send_file(pdf_path, mimetype='application/pdf', as_attachment=False)

# -----------------------------
# 8) ROUTE ANNULATION PAIEMENT
# -----------------------------
@app.route("/payment-cancel")
def payment_cancel():
    """
    Si l'utilisateur annule le paiement.
    """
    return render_template("cancel.html")

# -----------------------------
# 9) GENERATION DE LA BD
# -----------------------------
def generate_comic_book(data):
    """
    Génère une BD personnalisée avec GPT et DALL-E
    """
    name = data.get("name")
    age = data.get("age")
    physical = data.get("appearance")
    style = data.get("style")
    ambiance = data.get("ambiance")
    theme = data.get("theme", "aventure")

    # Prompt pour GPT
    gpt_prompt = (
        f"Crée une histoire en texte pour bande dessinée EN FRANÇAIS sur le thème '{theme}' en 10 planches distinctes avec un personnage principal "
        f"nommé {name}, âgé de {age} ans, ayant ces caractéristiques: {physical}. Style visuel: {style}. Ambiance: {ambiance}.\n\n"
        "Pour chaque planche (numérotée de 1 à 10), génère EXACTEMENT le texte suivant qui sera directement envoyé à DALL-E 3 :\n\n"
        "PROMPT_DALL_E_PLANCHE_1:\n"
        f"Bande dessinée style {style}, ambiance {ambiance}, EN FRANÇAIS. [Description détaillée de la scène]. "
        f"Personnage principal: {name}, {age} ans, {physical}. "
        "TITRE EN FRANÇAIS: [titre de la planche]. "
        "DIALOGUE EN FRANÇAIS: [court dialogue dans une bulle]. "
        "NARRATION EN FRANÇAIS: [courte narration en bas]."
        "\n\n"
        "Répète ce format exact pour chaque planche de 1 à 10, en marquant clairement le début de chaque prompt avec 'PROMPT_DALL_E_PLANCHE_X:'. "
        "Assure-toi que l'histoire progresse logiquement et garde une cohérence narrative entre les planches. "
        "Limite les dialogues et narrations à maximum 15 mots chacun. "
        "Les descriptions doivent être riches en détails visuels. "
        "N'inclus AUCUN texte explicatif entre les prompts. "
        "TOUT LE TEXTE DOIT ÊTRE EN FRANÇAIS ET NE PAS UTILISER DE VOCABULAIRE VULGAIRE QUI ENFREINT LA POLITIQUE DE OPENAI."
    )

    try:
        # 1) GPT => extraction des prompts pour DALL-E
        response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": gpt_prompt}],
        max_tokens=3000,
        temperature=0.7,
        )

        gpt_text = response.choices[0].message.content.strip()


        # Extraire les prompts
        prompt_pattern = r"PROMPT_DALL_E_PLANCHE_(\d+):(.*?)(?=PROMPT_DALL_E_PLANCHE_\d+:|$)"
        matches = re.finditer(prompt_pattern, gpt_text, re.DOTALL)
        prompts_dalle = []
        for match in matches:
            num = int(match.group(1))
            prompt_text = match.group(2).strip()
            prompts_dalle.append((num, prompt_text))

        # Trier par ordre
        prompts_dalle.sort(key=lambda x: x[0])
    except Exception as e:
        print("Erreur GPT:", e)
        return None

    # 2) Générer les images avec DALL-E
    temp_dir = tempfile.mkdtemp()
    image_files = []
    for idx, prompt_text in prompts_dalle:
        try:
            final_prompt = (
                "IMPORTANT: TOUT LE TEXTE INSÉRÉ DANS L'IMAGE (dialogues, etc.) "
                "DOIT ÊTRE STRICTEMENT EN FRANÇAIS.\n"
                + prompt_text
            )
            dalle_response = client.images.generate(
            model="dall-e-3",
            prompt=final_prompt,
            n=1,
            size="1024x1024"
            )

            image_url = dalle_response.data[0].url

            img_data = requests.get(image_url).content

            image_path = os.path.join(temp_dir, f"page_{idx}.png")
            with open(image_path, "wb") as f:
                f.write(img_data)
            image_files.append((idx, image_path))
        except Exception as e:
            print(f"Erreur DALL-E planche {idx}:", e)
            continue  # Continue avec les autres pages même si une échoue

    # Si aucune image n'a été générée, on arrête
    if not image_files:
        print("Aucune image n'a été générée correctement")
        return None

    image_files.sort(key=lambda x: x[0])
    sorted_image_files = [path for _, path in image_files]

    # 3) Création du PDF
    pdf = FPDF()
    pdf.set_auto_page_break(0)

    # Page de couverture
    pdf.add_page()
    pdf.set_font("Arial", "B", 24)
    pdf.cell(0, 40, "BD Personnalisée", 0, 1, "C")
    pdf.set_font("Arial", "B", 28)
    pdf.cell(0, 20, name, 0, 1, "C")
    pdf.set_font("Arial", "", 16)
    pdf.cell(0, 15, f"Thème : {theme}", 0, 1, "C")
    pdf.cell(0, 15, f"Style : {style}", 0, 1, "C")
    pdf.cell(0, 15, f"Ambiance : {ambiance}", 0, 1, "C")
    pdf.set_font("Arial", "I", 12)
    pdf.cell(0, 40, "Généré avec IA", 0, 1, "C")

    # Pages avec les planches de BD
    for img_file in sorted_image_files:
        pdf.add_page()
        pdf.image(img_file, x=10, y=10, w=190)

    pdf_path = os.path.join(temp_dir, "bd_personnalisee.pdf")
    pdf.output(pdf_path)

    return pdf_path

# -----------------------------
# 9) LANCEMENT DU SERVEUR
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)