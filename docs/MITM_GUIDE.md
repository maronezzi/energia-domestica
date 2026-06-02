# Tuya Local Key via MITMproxy — Guia Completo

## O que é

Capturar o tráfego de rede do app **Smart Life** no celular usando um proxy MITM no PC.
Omitmproxy intercepta as requisições HTTPS e expõe a `localKey` que o app recebe da cloud Tuya.

**Vantagem:** Não precisa de Tuya Cloud, não precisa de Tuya IoT, não precisa de root.
**Funciona:** Android e iOS (com certificado CA).

---

## Instalação (já feita no seu PC)

```bash
pip install mitmproxy --break-system-packages
mitmdump --version
```

---

## PASSO 1 — Exportar certificado MITM pro celular

### No PC:
```bash
mitmdump
```
O certificado vai ser gerado em:
```
~/.mitmproxy/mitmproxy-ca-cert.pem   (Linux)
```

### Transferir pro celular:
```bash
# Via email, WhatsApp, ou scp
scp ~/.mitmproxy/mitmproxy-ca-cert.pem celular:/Download/
```

### No Android (instalar o certificado):
1. **Configurações → Segurança → Criptografia e credenciais**
2. **Instalar de armazenamento interno**
3. Navegue até `Download/mitmproxy-ca-cert.pem`
4. Nome: `MITM Proxy CA`
5. WiFi → selecione "WiFi e apps" (pra capturar tráfego de apps)
6. Confirme

### No iOS:
1. Baixa o `.pem` pelo Safari
2. **Configurações → Geral → VPN e Gerenciamento de Dispositivos**
3. Instala o perfil
4. **Configurações → Sobre → Certificados** → ativa confiança total

---

## PASSO 2 — Configurar proxy no celular

No celular, configure o proxy WiFi:

| Campo | Valor |
|-------|-------|
| Proxy | **Manual** |
| Nome do host | IP do seu PC (veja abaixo) |
| Porta | **8080** |

Para descobrir o IP do PC:
```bash
python3 -c "import socket; s=socket.socket(); s.connect(('8.8.8.8',80); print(s.getsockname()[0])"
```

Exemplo: `192.168.1.42`

---

## PASSO 3 — Rodar o captura

```bash
python3 mitm_capture.py --wizard
# ou simplesmente:
python3 mitm_capture.py
```

**O que acontece:**
- O script inicia o mitmdump na porta 8080
- Filtra automaticamente requisições da Tuya
- Captura `localKey` de todas as respostas
- Salva em `tuya_keys_captured.json`

---

## PASSO 4 — Gerar as keys

1. **Celular com proxy configurado** → abre o app Smart Life
2. **Navega pelos dispositivos** — toca em cada um pra forçar uma requisição
3. **Volta no PC** → as keys aparecem no terminal
4. **Ctrl+C** → salva o arquivo JSON

---

## PASSO 5 — Usar as keys no tuya_monitor.py

```bash
# Ver keys capturadas
python3 mitm_capture.py --show-keys

# Adicionar ao tuya_monitor.py
# Edite tuya_devices.json e adicione o campo "key":
```

```json
[
  {
    "ip": "192.168.1.101",
    "id": "your_device_id_here",
    "key": "your_22_char_key_here",
    "version": "3.5",
    "name": "Medidor Principal"
  }
]
```

```bash
# Agora monitora!
python3 tuya_monitor.py --monitor \
    --id your_device_id_here \
    --ip 192.168.1.101 \
    --key your_22_char_key_here \
    --interval 5 --csv
```

---

## Solução de problemas

| Problema | Solução |
|----------|---------|
| "No route to host" | Celular e PC na mesma rede WiFi |
| Celular não conecta no proxy | Desabilite VPN no celular |
| Nenhuma key capturada | Abra Smart Life e toque nos dispositivos |
| App Smart Life não abre com proxy | Confirme que o certificado CA está instalado e confiado |
| Timeout | Aumente com `--timeout 300` |
| Porto 8080 em uso | Mude com `--port 8081` |

---

## Alternative: mitmweb (interface gráfica)

```bash
mitmweb --listen-port 8080 --web-interface-host 0.0.0.0
```
Abre uma interface web em `http://localhost:8081` — mais fácil de filtrar visualmente.

---

## Fluxo resumido

```
PC (mitmdump :8080) ←─── proxy HTTP ──── Celular (Smart Life)
        │
        └── intercepta HTTPS da Tuya
                │
                └── extrai "localKey"
                        │
                        └── salva em tuya_keys_captured.json
                                │
                                └── usado no tuya_monitor.py
```

## Arquivos gerados

- `tuya_devices.json` — seus dispositivos (IPs e IDs)
- `tuya_keys_captured.json` — keys capturadas
- `energy_log_*.csv` — dados de energia lidos
