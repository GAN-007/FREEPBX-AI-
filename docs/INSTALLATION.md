# Asterisk AI Voice Agent - Installation Guide (v4.2)

This guide provides detailed instructions for setting up the Asterisk AI Voice Agent v4.2 on your server.

## Two Setup Paths

Choose the path that best fits your experience level:

### Path A: Interactive Quickstart (Recommended for First-Time Users)

**5-minute guided setup** with automatic validation:

```bash
git clone https://github.com/hkjarral/Asterisk-AI-Voice-Agent.git
cd Asterisk-AI-Voice-Agent

# Run installer (sets up Docker, offers CLI installation)
./install.sh

# After installation, run interactive wizard
agent quickstart
```

The wizard will:
1. ✅ Guide you through provider selection (OpenAI, Deepgram, Google, Local)
2. ✅ Validate your API keys before saving
3. ✅ Test Asterisk ARI connection
4. ✅ Generate dialplan configuration
5. ✅ Show clear next steps

**Best for:** First-time users, quick deployments, validation testing

---

### Path B: Manual Setup (Advanced Users)

**Traditional installer** with manual configuration:

```bash
git clone https://github.com/hkjarral/Asterisk-AI-Voice-Agent.git
cd Asterisk-AI-Voice-Agent
./install.sh
```

The installer will:
1. Guide you through **3 golden baseline choices**:
   - **OpenAI Realtime** - Fastest (0.5-1.5s), requires OPENAI_API_KEY
   - **Deepgram Voice Agent** - Enterprise (1-2s), requires DEEPGRAM_API_KEY + OPENAI_API_KEY
   - **Local Hybrid** - Privacy-focused (3-7s), requires OPENAI_API_KEY + 8GB RAM
2. Validate ARI connection with your Asterisk server
3. Prompt for required API keys
4. Offer CLI tool installation
5. Start Docker containers automatically

**Best for:** Advanced users, custom configurations, specific requirements

---

## Detailed Installation

For manual installation, custom configurations, or troubleshooting, continue below.

## 1. Prerequisites

Before you begin, ensure your system meets the following requirements:

- **Operating System**: A modern Linux distribution (e.g., Ubuntu 20.04+, CentOS 7+).
- **Asterisk**: Version 18 or newer. FreePBX 15+ is also supported.
- **ARI (Asterisk REST Interface)**: Enabled and configured on your Asterisk server.
- **Docker**: Latest stable version of Docker and Docker Compose.
- **Git**: Required to clone the project repository.
- **Network Access**: Your server must be able to make outbound connections to the internet for Docker image downloads and API access to AI providers.

### Prerequisite checks

- Verify required Asterisk modules are loaded:

  ```bash
  asterisk -rx "module show like res_ari_applications"
  asterisk -rx "module show like app_audiosocket"
  ```

  Expected example output:

  ```
  Module                         Description                               Use Count  Status   Support Level
  res_ari_applications.so        RESTful API module - Stasis application   0          Running  core
  1 modules loaded

  Module                         Description                               Use Count  Status   Support Level
  app_audiosocket.so             AudioSocket Application                    20         Running  extended
  1 modules loaded
  ```

  If Asterisk < 18, on FreePBX Distro run:

  ```bash
  asterisk-switch-version   # aka asterisk-version-switch
  ```

  and select Asterisk 18+.

- Quick install Docker
  - Ubuntu:

    ```bash
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker $USER && newgrp docker
    docker --version && docker compose version
    ```

  - CentOS/Rocky/Alma:

    ```bash
    sudo dnf -y install dnf-plugins-core
    sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    sudo dnf install -y docker-ce docker-ce-cli containerd.io
    sudo systemctl enable --now docker
    docker --version && docker compose version
    ```

## 2. Installation Steps

The installation is handled by an interactive script that will guide you through the process.

### Step 2.1: Clone the Repository

First, clone the project repository to a directory on your server.

```bash
git clone https://github.com/hkjarral/Asterisk-AI-Voice-Agent.git
cd Asterisk-AI-Voice-Agent
```

### Step 2.2: Run the Installation Script

Execute the `install.sh` script. You will need to run it with `sudo` if your user does not have permissions to run Docker.

```bash
./install.sh
```

The script will perform the following actions:

1. **System Checks**: Verify that Docker is installed and running.
2. **Interactive Setup**: Launch a wizard to collect configuration details.

### Step 2.3: Interactive Setup Wizard

The wizard will prompt you for the following information.

#### AI Provider Selection

You will be asked to choose an AI provider.

- **[1] OpenAI Realtime (Default, GA)**: Out-of-the-box realtime voice path.
- **[2] Deepgram Voice Agent**: Cloud STT/TTS with strong latency/quality.
- **[3] Local Models**: Offline option (Vosk STT, TinyLlama/other LLM, Piper TTS).

#### Provider Configuration

Based on your selection, you will need to provide API keys.

- **Deepgram API Key**: Required if you select the Deepgram provider.
- **OpenAI API Key**: Required if you select any OpenAI-based pipeline.

#### Asterisk ARI Configuration

You will need to provide the connection details for your Asterisk server's ARI.

- **Asterisk Host**: The hostname or IP address of your Asterisk server.
- **ARI Username**: The username for an ARI user.
- **ARI Password**: The password for the ARI user.

### What You'll Need (at a glance)

- A Linux server with Docker + Docker Compose
- Asterisk 18+ or FreePBX 15+ with ARI enabled
- API keys for your chosen provider (optional): `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`

### Step 2.4: Configuration File Generation

After you complete the wizard, the script will create a `.env` file in the project root with all your settings. You can manually edit this file later if you need to make changes.

### Step 2.5: Start the Service

Once the configuration is complete, the script will prompt you to build and start the Docker container. You can also do this manually.

```bash
docker-compose up --build -d
```

> IMPORTANT: First startup time (local models)
>
> If you selected a Local or Hybrid workflow, the `local-ai-server` may take 15–20 minutes on first startup to load LLM/TTS models depending on your CPU, RAM, and disk speed. This is expected and readiness may show degraded until models have fully loaded. Monitor with:
>
> ```bash
> docker-compose logs -f local-ai-server
> ```
>
> Subsequent restarts are typically much faster due to OS page cache. If startup is too slow for your hardware, consider using MEDIUM or LIGHT tier models and update the `.env` model paths accordingly.

## 3. Verifying the Installation

After starting the service, you can check that it is running correctly.

### Check Docker Container Status

```bash
docker-compose ps
```

You should see the `ai-engine` and `local-ai-server` containers running.

### Check Container Logs

```bash
docker-compose logs -f ai-engine
```

Look for a message indicating a successful connection to the Asterisk ARI and that the AudioSocket listener is ready on port 8090.

### 5. Configure Asterisk Dialplan

The engine uses **ARI-based architecture** - the dialplan just hands calls to Stasis. The engine manages audio transport internally.

**Minimal Dialplan** (works for all 3 golden baselines):

Add to `/etc/asterisk/extensions_custom.conf`:

```asterisk
[from-ai-agent]
exten => s,1,NoOp(Asterisk AI Voice Agent v4.0)
 same => n,Stasis(asterisk-ai-voice-agent)
 same => n,Hangup()
```

**Optional: Provider Override via Channel Variables**:

```asterisk
[from-ai-agent-support]
exten => s,1,NoOp(AI Agent - Customer Support)
 same => n,Set(AI_PROVIDER=deepgram)
 same => n,Set(AI_CONTEXT=support)
 same => n,Stasis(asterisk-ai-voice-agent)
 same => n,Hangup()

[from-ai-agent-openai]
exten => s,1,NoOp(AI Agent - OpenAI Realtime)
 same => n,Set(AI_PROVIDER=openai_realtime)
 same => n,Stasis(asterisk-ai-voice-agent)
 same => n,Hangup()
```

**Important:** Do NOT use `AudioSocket()` in the dialplan. The engine originates AudioSocket channels via ARI automatically.

**How It Works:**
1. Call enters `Stasis(asterisk-ai-voice-agent)`
2. Engine receives StasisStart event via ARI
3. **For full agents** (OpenAI, Deepgram): Engine creates AudioSocket channel via ARI
4. **For hybrid pipelines** (Local Hybrid): Engine creates ExternalMedia RTP channel via ARI
5. Engine bridges transport channel with caller
6. Two-way audio flows automatically

After adding the dialplan, reload Asterisk configuration:

```bash
asterisk -rx "dialplan reload"
```

## 4. Troubleshooting

- **Cannot connect to ARI**:
  - Verify that your Asterisk `host`, `username`, and `password` are correct in the `.env` file.
  - Ensure that the ARI port (usually 8088) is accessible from the Docker container.
  - Check your `ari.conf` and `http.conf` in Asterisk.
- **AI does not respond**:
  - Check that your API keys in the `.env` file are correct.
- **Audio Quality Issues**:
  - Confirm AudioSocket is connected (see Asterisk CLI and `ai-engine` logs).
  - Use a tmpfs for media files (e.g., `/mnt/asterisk_media`) to minimize I/O latency for file-based playback.
  - Verify you are not appending file extensions to ARI `sound:` URIs (Asterisk will add them automatically).

- **No host Python 3 installed (scripts/Makefile)**:
  - The Makefile auto-falls back to running helper scripts inside the `ai-engine` container. You’ll see a hint when it does.
  - Check your environment:

        ```bash
        make check-python
        ```

  - Run helpers directly in the container if desired:

        ```bash
        docker-compose exec -T ai-engine python /app/scripts/validate_externalmedia_config.py
        docker-compose exec -T ai-engine python /app/scripts/test_externalmedia_call.py
        docker-compose exec -T ai-engine python /app/scripts/monitor_externalmedia.py
        docker-compose exec -T ai-engine python /app/scripts/capture_test_logs.py --duration 40
        docker-compose exec -T ai-engine python /app/scripts/analyze_logs.py /app/logs/latest.json
        ```

For more advanced troubleshooting, refer to the project's main `README.md` or open an issue in the repository.
