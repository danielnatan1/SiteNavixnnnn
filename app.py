from flask import Flask, render_template, request, redirect, url_for, session, flash
from pymongo import MongoClient
from bson.objectid import ObjectId
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
import requests
import os
import logging
import bcrypt
from datetime import datetime

# Configuração de logs
logging.basicConfig(level=logging.DEBUG)
logging.getLogger('pymongo').setLevel(logging.WARNING)

# Carregar variáveis de ambiente
load_dotenv()

# Inicialização da aplicação Flask
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "chave_teste_123")

# Conexão com MongoDB
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/"))
db = client.meubanco
usuarios = db.usuarios
anotacoes = db.anotacoes
financeiro = db.financeiro

# Credenciais da API Navix
USUARIO_API = os.getenv("API_USER", "81")
SENHA_API = os.getenv("API_PASS", "29f15a48f2f967a8bcf72662adee82953d9dc1886efe8a4704759c3e787da18f")
URL_API_CLIENTE = "https://navixtelecom.com.br/webservice/v1/cliente"
HEADERS = {"ixcsoft": "listar", "Content-Type": "application/json"}


# ------------------------
# Funções auxiliares
# ------------------------

def usuario_atual():
    email = session.get('usuario')
    if email:
        return usuarios.find_one({'email': email})
    return None


def gerar_hash_bcrypt(senha):
    return bcrypt.hashpw(senha.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def verificar_senha_bcrypt(hash_armazenado, senha_digitada):
    try:
        return bcrypt.checkpw(senha_digitada.encode('utf-8'), hash_armazenado.encode('utf-8'))
    except Exception as e:
        app.logger.error(f"Erro ao verificar senha: {e}")
        return False


def buscar_cliente_por_id(cliente_id):
    payload = {
        "qtype": "cliente.id",
        "query": str(cliente_id),
        "oper": "=",
        "page": "1",
        "rp": "20",
        "sortname": "cliente.id",
        "sortorder": "desc"
    }
    try:
        response = requests.post(URL_API_CLIENTE, headers=HEADERS, json=payload,
                                 auth=HTTPBasicAuth(USUARIO_API, SENHA_API), timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        app.logger.error(f"Erro na requisição cliente: {e}")
    return None


def buscar_situacao_financeira(cliente_id, id_contrato=None):
    url = "https://navixtelecom.com.br/webservice/v1/fn_areceber"
    regras = [{"qtype": "fn_areceber.id_cliente", "query": str(cliente_id), "oper": "="}]
    if id_contrato:
        regras.append({"qtype": "fn_areceber.id_contrato", "query": str(id_contrato), "oper": "="})

    payload = {
        "search": "true",
        "page": "1",
        "rp": "100",
        "sortname": "fn_areceber.id",
        "sortorder": "desc",
        "rules": regras
    }

    try:
        response = requests.post(url, headers=HEADERS, json=payload,
                                 auth=HTTPBasicAuth(USUARIO_API, SENHA_API), timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get("registros", [])
    except Exception as e:
        app.logger.error(f"Erro na consulta fn_areceber: {e}")
    return []


def calcular_resumo_financeiro(faturas):
    resumo = {"valor_aberto": 0.0, "qtd_aberto": 0, "valor_vencido": 0.0, "qtd_vencido": 0}
    hoje = datetime.today()

    for f in faturas:
        status = f.get('status_descricao', '').lower()
        try:
            valor = float(f.get('valor', 0))
        except (ValueError, TypeError):
            valor = 0.0
        try:
            venc = datetime.strptime(f.get('data_vencimento'), "%Y-%m-%d")
        except (ValueError, TypeError):
            venc = None

        if status in ['aberto', 'atrasado']:
            resumo['valor_aberto'] += valor
            resumo['qtd_aberto'] += 1
            if venc and venc < hoje:
                resumo['valor_vencido'] += valor
                resumo['qtd_vencido'] += 1

    resumo['valor_aberto'] = round(resumo['valor_aberto'], 2)
    resumo['valor_vencido'] = round(resumo['valor_vencido'], 2)
    return resumo


def buscar_contratos_do_cliente(cliente_id):
    url = "https://navixtelecom.com.br/webservice/v1/cliente_contrato"
    payload = {
        "qtype": "cliente_contrato.id_cliente",
        "query": str(cliente_id),
        "oper": "=",
        "page": "1",
        "rp": "100",
        "sortname": "cliente_contrato.id",
        "sortorder": "asc"
    }

    try:
        response = requests.post(url, headers=HEADERS, json=payload,
                                 auth=HTTPBasicAuth(USUARIO_API, SENHA_API), timeout=10)
        response.raise_for_status()
        data = response.json()
        contratos = list(data.get("registros", {}).values())
        app.logger.info(f"Contratos encontrados para cliente {cliente_id}: {contratos}")
        return contratos
    except Exception as e:
        app.logger.error(f"Erro ao buscar contratos: {e}")
        return []


# ------------------------
# Rotas da aplicação
# ------------------------

@app.route("/")
def index():
    if 'usuario' in session:
        return redirect(url_for('buscar_cliente'))
    return redirect(url_for('login'))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip()
        senha = request.form["senha"]
        user = usuarios.find_one({"email": email})
        if user and verificar_senha_bcrypt(user["senha"], senha):
            session["usuario"] = email
            session["user_id"] = str(user["_id"])
            session["is_admin"] = user.get("is_admin", False)
            flash("Login realizado com sucesso.", "success")
            return redirect(url_for("buscar_cliente"))
        flash("Usuário ou senha incorretos.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Sessão encerrada.", "success")
    return redirect(url_for("login"))


@app.route("/alterar_senha", methods=["GET", "POST"])
def alterar_senha():
    if 'user_id' not in session:
        flash("Você precisa estar logado.", "warning")
        return redirect(url_for("login"))

    user = usuarios.find_one({"_id": ObjectId(session["user_id"])})
    if not user:
        flash("Usuário não encontrado.", "danger")
        return redirect(url_for("login"))

    if request.method == "POST":
        atual = request.form["senha_atual"]
        nova = request.form["nova_senha"]
        confirmar = request.form["confirmar_senha"]

        if not verificar_senha_bcrypt(user["senha"], atual):
            flash("Senha atual incorreta.", "danger")
        elif nova != confirmar:
            flash("Nova senha e confirmação não coincidem.", "danger")
        else:
            nova_hash = gerar_hash_bcrypt(nova)
            usuarios.update_one({"_id": user["_id"]}, {"$set": {"senha": nova_hash}})
            flash("Senha alterada com sucesso!", "success")
            return redirect(url_for("index"))

    return render_template("alterar_senha.html")


@app.route("/buscar_cliente", methods=["GET", "POST"])
def buscar_cliente():
    if "usuario" not in session:
        flash("Faça login para continuar.", "warning")
        return redirect(url_for("login"))

    if request.method == "POST":
        cliente_id = request.form.get("cliente_id", "").strip()
        if not cliente_id:
            flash("Informe o ID do cliente.", "warning")
            return render_template("buscar_cliente.html")

        dados = buscar_cliente_por_id(cliente_id)
        if dados and dados.get("registros"):
            cliente = dados["registros"][0]

            anot = list(anotacoes.find({"cliente_id": cliente_id}).sort("created_at", -1))
            faturas = buscar_situacao_financeira(cliente_id)
            resumo = calcular_resumo_financeiro(faturas)

            return render_template(
                "mostrar_cliente.html",
                cliente=cliente,
                anotacoes=anot,
                faturas=faturas,
                resumo=resumo
            )
        else:
            flash("Cliente não encontrado.", "danger")
    return render_template("buscar_cliente.html")


@app.route("/anotar/<cliente_id>", methods=["POST"])
def anotar(cliente_id):
    if "usuario" not in session:
        flash("Faça login para continuar.", "warning")
        return redirect(url_for("login"))

    texto = request.form.get("texto", "").strip()
    if not texto:
        flash("Anotação vazia não salva.", "warning")
        return redirect(url_for("buscar_cliente"))

    anotacoes.insert_one({
        "cliente_id": cliente_id,
        "texto": texto,
        "usuario": session["usuario"],
        "created_at": datetime.now()
    })
    flash("Anotação salva com sucesso.", "success")
    return redirect(url_for("buscar_cliente"))


@app.route("/mostrar_cliente/<cliente_id>")
def mostrar_cliente(cliente_id):
    if "usuario" not in session:
        flash("Faça login para continuar.", "warning")
        return redirect(url_for("login"))

    filtros = {
        "qtype": "id_cliente",
        "query": cliente_id,
        "oper": "=",
        "page": 1,
        "rp": 50,
        "sortname": "id",
        "sortorder": "asc"
    }
    app.logger.info(f"🔑 Filtros usados na requisição de contratos: {filtros}")

    url = "https://navixtelecom.com.br/webservice/v1/cliente_contrato"
    try:
        response = requests.post(url, headers=HEADERS, json=filtros,
                                 auth=HTTPBasicAuth(USUARIO_API, SENHA_API), timeout=10)
        response.raise_for_status()
        data = response.json()
        contratos = list(data.get("registros", {}).values())
    except Exception as e:
        app.logger.error(f"Erro ao buscar contratos: {e}")
        contratos = []

    cliente = buscar_cliente_por_id(cliente_id)
    cliente_info = cliente["registros"][0] if cliente and cliente.get("registros") else None

    return render_template("mostrar_cliente.html", cliente=cliente_info, contratos=contratos)


@app.route("/contratos/<cliente_id>")
def contratos(cliente_id):
    if "usuario" not in session:
        flash("Faça login para continuar.", "warning")
        return redirect(url_for("login"))

    url = "https://navixtelecom.com.br/webservice/v1/cliente_contrato"
    payload = {
        "qtype": "cliente_contrato.id_cliente",
        "query": str(cliente_id),
        "oper": "=",
        "page": "1",
        "rp": "100",
        "sortname": "cliente_contrato.id",
        "sortorder": "asc"
    }

    try:
        response = requests.post(url, headers=HEADERS, json=payload,
                                 auth=HTTPBasicAuth(USUARIO_API, SENHA_API), timeout=10)
        response.raise_for_status()
        data = response.json()
        contratos = list(data.get("registros", {}).values())
    except Exception as e:
        app.logger.error(f"Erro ao buscar contratos do cliente: {e}")
        contratos = []

    return render_template("contratos.html", contratos=contratos, cliente_id=cliente_id)


@app.route("/ver_contrato")
def ver_contrato():
    if "usuario" not in session:
        flash("Faça login para continuar.", "warning")
        return redirect(url_for("login"))

    contrato_id = request.args.get("id")
    cliente_id = request.args.get("cliente_id")
    if not contrato_id or not cliente_id:
        flash("Contrato ou cliente inválido.", "danger")
        return redirect(url_for("buscar_cliente"))

    url = f"https://navixtelecom.com.br/webservice/v1/cliente_contrato/{contrato_id}"
    try:
        response = requests.get(url, headers=HEADERS,
                                auth=HTTPBasicAuth(USUARIO_API, SENHA_API), timeout=10)
        response.raise_for_status()
        contrato = response.json()
    except Exception as e:
        app.logger.error(f"Erro ao buscar contrato: {e}")
        flash("Erro ao buscar contrato.", "danger")
        return redirect(url_for("contratos", cliente_id=cliente_id))

    return render_template('contrato.html', contrato=contrato, cliente_id=cliente_id)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        senha = request.form.get('senha')

        if not email or not senha:
            flash('Por favor, preencha todos os campos.', 'danger')
            return redirect(url_for('register'))

        # Verifica se já existe usuário com o mesmo email
        if mongo.db.users.find_one({'email': email}):
            flash('Este e-mail já está cadastrado.', 'warning')
            return redirect(url_for('register'))

        # Criptografa a senha
        senha_hash = bcrypt.generate_password_hash(senha).decode('utf-8')

        # Cria o usuário no banco
        mongo.db.users.insert_one({
            'email': email,
            'senha': senha_hash,
            'is_admin': False
        })

        flash('Conta criada com sucesso! Faça login.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


if __name__ == "__main__":
    app.run(debug=True)
