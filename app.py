"""
API Localizador - Recebe localização e serve mapa
Configurado para deploy no Render.com
"""
import json
import os
import random
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder="static")
CORS(app, resources={r"/api/*": {"origins": "*", "methods": ["GET", "POST", "OPTIONS"], "allow_headers": ["Content-Type"]}})

# Armazena as últimas localizações por device_id
localizacoes = {}
# Histórico de localizações por dispositivo (para o rastro)
historico = {}
# Cache de endereços para evitar muitas requisições ao Nominatim
cache_enderecos = {}
# Arquivo de nomes dos dispositivos
nomes_file = Path(__file__).parent / "nomes_dispositivos.json"
# Arquivo de histórico por dispositivo (persistido localmente)
historico_file = Path(__file__).parent / "historico.json"


@dataclass
class Localizacao:
    device_id: str
    lat: float
    lng: float
    timestamp: float
    endereco: str = ""
    bateria: float = None

    def to_dict(self):
        d = {
            "device_id": self.device_id,
            "lat": self.lat,
            "lng": self.lng,
            "timestamp": self.timestamp,
            "endereco": self.endereco,
        }
        if self.bateria is not None:
            d["bateria"] = self.bateria
        return d


def carregar_nomes():
    """Carrega nomes dos dispositivos do arquivo"""
    if nomes_file.exists():
        try:
            with open(nomes_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def salvar_nomes(nomes: dict):
    """Salva nomes dos dispositivos no arquivo"""
    with open(nomes_file, "w", encoding="utf-8") as f:
        json.dump(nomes, f, ensure_ascii=False, indent=2)


def carregar_historico() -> dict:
    """Carrega histórico dos dispositivos do arquivo"""
    if historico_file.exists():
        try:
            with open(historico_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def salvar_historico() -> None:
    """Salva histórico no arquivo (últimos 2000 por dispositivo)"""
    try:
        limitado = {
            did: (pontos[-2000:] if len(pontos) > 2000 else pontos)
            for did, pontos in historico.items()
        }
        with open(historico_file, "w", encoding="utf-8") as f:
            json.dump(limitado, f, ensure_ascii=False, indent=0)
    except Exception:
        pass


# Carrega histórico do arquivo (após definição das funções)
historico.update(carregar_historico())


def _formatar_endereco_simples(addr: dict) -> str:
    """Extrai rua, bairro e cidade do retorno do Nominatim.
    Prioridade para bairro: neighbourhood (mais local) antes de suburb (mais amplo)."""
    if not addr:
        return "Endereço não encontrado"
    rua = addr.get("road") or addr.get("street") or addr.get("pedestrian") or ""
    # Bairro: neighbourhood é mais específico; suburb pode ser região maior
    bairro = (
        addr.get("neighbourhood")
        or addr.get("suburb")
        or addr.get("quarter")
        or addr.get("city_district")
        or addr.get("district")
        or addr.get("residential")
        or ""
    )
    cidade = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("municipality")
        or addr.get("county")
        or ""
    )
    partes = [p for p in (rua, bairro, cidade) if p]
    return ", ".join(partes) if partes else "Endereço não encontrado"


def reverse_geocode(lat: float, lng: float) -> str:
    """Obtém endereço (rua, bairro, cidade) a partir de coordenadas usando Nominatim.
    Usa zoom=16 para melhor precisão de rua/bairro (evita matching em pontos distantes)."""
    cache_key = f"{lat:.4f}_{lng:.4f}"
    if cache_key in cache_enderecos:
        return cache_enderecos[cache_key]

    try:
        # zoom=16: ruas principais e secundárias, melhor matching de bairro
        url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lng}&format=json&zoom=16&addressdetails=1&accept-language=pt"
        headers = {"User-Agent": "LocalizadorApp/1.0"}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            addr = data.get("address", {})
            endereco = _formatar_endereco_simples(addr)
            cache_enderecos[cache_key] = endereco
            return endereco
    except Exception:
        pass
    return "Endereço não disponível"


@app.route("/api/ping")
def ping():
    """End-point para testar se a API está acessível"""
    return jsonify({"ok": True, "mensagem": "API online"})


@app.route("/api/localizacao", methods=["POST"])
def receber_localizacao():
    """Recebe localização enviada pelo app"""
    try:
        data = request.get_json()
        if not data or "lat" not in data or "lng" not in data:
            return jsonify({"erro": "Dados inválidos"}), 400

        device_id = data.get("device_id", "dispositivo_1")
        lat = float(data["lat"])
        lng = float(data["lng"])
        timestamp = data.get("timestamp", time.time())
        bateria = data.get("bateria")
        if bateria is not None:
            try:
                bateria = float(bateria)
                if bateria < 0 or bateria > 100:
                    bateria = None
            except (TypeError, ValueError):
                bateria = None

        loc = Localizacao(
            device_id=device_id,
            lat=lat,
            lng=lng,
            timestamp=timestamp,
            endereco="",
            bateria=bateria
        )
        localizacoes[device_id] = loc
        # Adiciona ao histórico (últimas 500 em memória, 2000 persistidos)
        if device_id not in historico:
            historico[device_id] = []
        historico[device_id].append({"lat": lat, "lng": lng, "timestamp": timestamp})
        historico[device_id] = historico[device_id][-2000:]
        salvar_historico()
        return jsonify({"ok": True, "mensagem": "Localização recebida"})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/api/localizacoes")
def listar_localizacoes():
    """Retorna todas as localizações atuais"""
    lista = [asdict(loc) for loc in localizacoes.values()]
    return jsonify(lista)


@app.route("/api/historico")
def lista_historico():
    """Retorna o histórico de localizações por dispositivo (para o rastro)"""
    return jsonify(historico)


@app.route("/api/nomes", methods=["GET"])
def obter_nomes():
    """Retorna os nomes personalizados dos dispositivos (com ícone e cor)"""
    return jsonify(carregar_nomes())


@app.route("/api/cadastrar", methods=["POST"])
def cadastrar_rastreador():
    """Cadastra novo rastreador com nome, ícone e cor. Retorna device_id (código) para usar no app."""
    try:
        data = request.get_json()
        nome = (data.get("nome") or "").strip()
        if not nome:
            return jsonify({"erro": "Nome obrigatório"}), 400
        icon = (data.get("icon") or "🚗").strip() or "🚗"
        color = (data.get("color") or "#00d4aa").strip() or "#00d4aa"
        device_id = "R" + str(random.randint(10000, 99999))
        nomes = carregar_nomes()
        while device_id in nomes:
            device_id = "R" + str(random.randint(10000, 99999))
        nomes[device_id] = {"nome": nome, "icon": icon, "color": color}
        salvar_nomes(nomes)
        return jsonify({"ok": True, "device_id": device_id, "nome": nome, "icon": icon, "color": color})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/api/dispositivo/<device_id>", methods=["DELETE"])
def remover_dispositivo(device_id):
    """Remove um dispositivo cadastrado (e seus dados)."""
    try:
        nomes = carregar_nomes()
        if device_id not in nomes:
            return jsonify({"erro": "Dispositivo não encontrado"}), 404
        del nomes[device_id]
        salvar_nomes(nomes)
        if device_id in localizacoes:
            del localizacoes[device_id]
        if device_id in historico:
            del historico[device_id]
            salvar_historico()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/api/nomes", methods=["POST"])
def salvar_nomes_api():
    """Salva o nome (e opcionalmente ícone/cor) de um dispositivo"""
    try:
        data = request.get_json()
        device_id = data.get("device_id")
        nome = (data.get("nome") or "").strip()
        icon = (data.get("icon") or "🚗").strip() or "🚗"
        color = (data.get("color") or "#00d4aa").strip() or "#00d4aa"
        if not device_id:
            return jsonify({"erro": "device_id obrigatório"}), 400
        nomes = carregar_nomes()
        info = nomes.get(device_id, {})
        if isinstance(info, dict):
            info["nome"] = nome or device_id
            info["icon"] = icon
            info["color"] = color
        else:
            info = {"nome": nome or device_id, "icon": icon, "color": color}
        nomes[device_id] = info
        salvar_nomes(nomes)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


def _nominatim_to_results(nom_data):
    """Converte resposta do Nominatim para formato unificado."""
    results = []
    for r in (nom_data or [])[:10]:
        lat, lon = r.get("lat"), r.get("lon")
        if lat is not None and lon is not None:
            results.append({
                "lat": float(lat),
                "lng": float(lon),
                "display_name": r.get("display_name", ""),
            })
    return results


def _photon_to_results(features):
    """Converte resposta do Photon para formato unificado."""
    results = []
    for f in (features or [])[:10]:
        coords = (f.get("geometry") or {}).get("coordinates")
        if not coords or len(coords) < 2:
            continue
        lng, lat = coords[0], coords[1]
        p = f.get("properties") or {}
        pts = [p.get("street") or p.get("name"), p.get("housenumber"), p.get("locality") or p.get("district"), p.get("city"), p.get("state")]
        label = ", ".join(str(x) for x in pts if x)
        if not label:
            label = p.get("name") or f"{lat}, {lng}"
        results.append({"lat": lat, "lng": lng, "display_name": label})
    return results


def _req_nominatim(params, headers=None):
    """Chama Nominatim com parâmetros."""
    h = {"User-Agent": "LocalizadorApp/1.0", "Accept-Language": "pt-BR"}
    if headers:
        h.update(headers)
    url = "https://nominatim.openstreetmap.org/search?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v)
    try:
        r = requests.get(url, headers=h, timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


@app.route("/api/geocode")
def geocode():
    """Busca endereços via Photon/Nominatim (evita CORS no navegador)"""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"erro": "Parâmetro q obrigatório"}), 400

    results = []
    cep = "".join(c for c in q if c.isdigit())
    viacep_data = None

    # 1. CEP: ViaCEP -> Nominatim ESTRUTURADO (rua, cidade, estado)
    if len(cep) == 8:
        try:
            r_cep = requests.get(f"https://viacep.com.br/ws/{cep}/json/", timeout=5)
            if r_cep.status_code == 200:
                viacep_data = r_cep.json()
                if not viacep_data.get("erro"):
                    street = viacep_data.get("logradouro") or ""
                    city = viacep_data.get("localidade") or ""
                    state = viacep_data.get("uf") or ""
                    if street and city:
                        nom = _req_nominatim({
                            "format": "json",
                            "street": street,
                            "city": city,
                            "state": state,
                            "country": "Brasil",
                            "limit": 10,
                        })
                        results = _nominatim_to_results(nom)
                    if not results and city:
                        nom = _req_nominatim({
                            "format": "json",
                            "city": city,
                            "state": state,
                            "country": "Brasil",
                            "limit": 10,
                        })
                        results = _nominatim_to_results(nom)
        except Exception:
            pass
        if not results and viacep_data:
            parts = [viacep_data.get("logradouro"), viacep_data.get("bairro"), viacep_data.get("localidade"), viacep_data.get("uf")]
            q = ", ".join(p for p in parts if p)

    def _photon_query_simples(texto):
        """Simplifica query para evitar 400 do Photon (ex: 'Rua X, Bairro, Cidade, SP' -> 'Rua X, Cidade')"""
        partes = [p.strip() for p in texto.split(",") if p.strip()]
        if len(partes) <= 2:
            return texto
        ultima = partes[-1]
        cidade = partes[-2] if len(ultima) == 2 else ultima
        return partes[0] + ", " + cidade

    q_photon = _photon_query_simples(q)

    # 2. Photon (query simplificada evita 400 Bad Request)
    if not results:
        try:
            url = f"https://photon.komoot.io/api/?q={quote(q_photon)}&limit=10"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                features = data.get("features") or []
                results = _photon_to_results(features)
        except Exception:
            pass

    # 3. Photon com bbox Brasil
    if not results:
        try:
            url = f"https://photon.komoot.io/api/?q={quote(q_photon)}&limit=10&bbox=-74,-33,-34,5"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                features = data.get("features") or []
                results = _photon_to_results(features)
        except Exception:
            pass

    # 4. Nominatim free-form
    if not results:
        nom = _req_nominatim({
            "format": "json",
            "q": q + ", Brasil",
            "countrycodes": "br",
            "limit": 10,
        })
        results = _nominatim_to_results(nom)

    # 5. Nominatim com query mais simples (só cidade)
    if not results and " " in q:
        partes = [p.strip() for p in q.split(",") if p.strip()]
        if len(partes) >= 2:
            q_simples = partes[-2] + ", " + partes[-1] + ", Brasil"
            nom = _req_nominatim({
                "format": "json",
                "q": q_simples,
                "countrycodes": "br",
                "limit": 10,
            })
            results = _nominatim_to_results(nom)

    # 6. Tentar sem "Brasil"
    if not results:
        nom = _req_nominatim({
            "format": "json",
            "q": q,
            "countrycodes": "br",
            "limit": 10,
        })
        results = _nominatim_to_results(nom)

    return jsonify({"results": results})


@app.route("/api/endereco/<device_id>")
def obter_endereco(device_id):
    """Retorna o endereço do dispositivo (reverse geocoding)"""
    if device_id not in localizacoes:
        return jsonify({"erro": "Dispositivo não encontrado"}), 404

    loc = localizacoes[device_id]
    if not loc.endereco:
        loc.endereco = reverse_geocode(loc.lat, loc.lng)

    resp = {
        "device_id": device_id,
        "endereco": loc.endereco,
        "lat": loc.lat,
        "lng": loc.lng,
    }
    if loc.bateria is not None:
        resp["bateria"] = loc.bateria
    return jsonify(resp)


@app.route("/favicon.ico")
def favicon():
    """Retorna favicon mínimo para evitar 404"""
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    return Response(png, mimetype="image/png")


@app.route("/")
def mapa():
    """Serve a página do mapa"""
    return send_from_directory(app.static_folder, "mapa.html")


@app.route("/<path:path>")
def arquivos_estaticos(path):
    return send_from_directory(app.static_folder, path)


if __name__ == "__main__":
    Path(app.static_folder).mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
