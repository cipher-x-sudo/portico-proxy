---
title: 'Setting Up and Running CTFd with Docker: Step-by-Step Guide'
date: '2025-09-28'
lastmod: '2025-09-28'
tags: ['ctf', 'docker', 'cybersecurity', 'tutorial', 'capture-the-flag']
draft: false
summary: 'A comprehensive guide to setting up CTFd (Capture The Flag framework) using Docker. Learn how to install Docker, clone CTFd, and run your own CTF platform with step-by-step commands and screenshots.'
images: ['/static/images/fuzzer-logo.png']
authors: ['default']
---
https://github.com/cipher-x-sudo/portico-proxy.git
# Setting Up and Running CTFd with Docker: Step-by-Step Guide

Welcome to this comprehensive guide on setting up CTFd (Capture The Flag framework) using Docker! CTFd is a popular platform for hosting cybersecurity competitions and educational challenges. This guide will walk you through the entire process from installation to running your own CTF platform.

## Table of Contents

1. [What is CTFd?](#what-is-ctfd)
2. [Prerequisites](#prerequisites)
3. [Installing Docker](#installing-docker)
4. [Setting Up CTFd](#setting-up-ctfd)
5. [Running CTFd](#running-ctfd)
6. [Verifying Installation](#verifying-installation)
7. [Troubleshooting](#troubleshooting)
8. [SSL Certificate Setup](#ssl-certificate-setup)
9. [Next Steps](#next-steps)

## What is CTFd?

CTFd is a Capture The Flag framework that allows you to host cybersecurity competitions, educational challenges, and training exercises. It provides:

- **Challenge Management** - Create and organize various types of challenges
- **User Management** - Handle registrations and team formations
- **Scoring System** - Automatic scoring and leaderboards
- **Admin Panel** - Comprehensive administration tools
- **API Support** - RESTful API for integrations

## Prerequisites

Before we begin, ensure you have:

- **Linux/Ubuntu system** (this guide uses Ubuntu)
- **Internet connection** for downloading packages
- **Basic command line knowledge**
- **Administrative privileges** (sudo access)

## Installing Docker

### Step 1: Check Docker Installation

First, let's check if Docker is already installed on your system:

<div className="code-header">Terminal</div>

```bash
docker
```

**Expected Output:**
If Docker is not installed, you'll see a message suggesting installation options:
- `sudo apt install docker`
- `sudo apt install docker.io`
- `sudo apt install podman-docker`

### Step 2: Install Docker and Docker Compose

Install Docker and Docker Compose using the following command:

<div className="code-header">Terminal</div>

```bash
sudo apt install docker.io docker-compose -y
```

**What this command does:**
- **`sudo apt install`** - Installs packages with administrative privileges
- **`docker.io`** - The Docker engine package
- **`docker-compose`** - Docker Compose for multi-container applications
- **`-y`** - Automatically answers "yes" to installation prompts

**Expected Output:**
The system will download and install Docker along with Docker Compose. You'll see installation progress and confirmation messages.

## Setting Up CTFd

### Step 3: Clone CTFd Repository

Navigate to your desired directory and clone the CTFd repository:

<div className="code-header">Terminal</div>

```bash
git clone https://github.com/CTFd/CTFd.git
```

**What this command does:**
- **`git clone`** - Creates a local copy of the repository
- **`https://github.com/CTFd/CTFd.git`** - The official CTFd repository URL

**Expected Output:**
Git will download the CTFd source code to your local machine. You'll see cloning progress and confirmation.

### Step 4: Navigate to CTFd Directory

Move into the CTFd directory:

<div className="code-header">Terminal</div>

```bash
cd CTFd
```

## Running CTFd

### Step 5: Start CTFd with Docker Compose

Launch CTFd using Docker Compose:

<div className="code-header">Terminal</div>

```bash
docker-compose up
```

**What this command does:**
- **`docker-compose up`** - Starts and runs a multi-container Docker application
- **Builds and starts** all services defined in docker-compose.yml
- **Runs in foreground** - You'll see real-time logs

**Expected Output:**
You'll see Docker downloading images, building containers, and starting services. The output will show:
- Image downloads (if not already cached)
- Container creation and startup
- Service initialization logs
- Database setup and migrations

### Step 6: Run in Background (Optional)

To run CTFd in the background, use:

<div className="code-header">Terminal</div>

```bash
docker-compose up -d
```

**What this does:**
- **`-d` flag** - Runs containers in detached mode (background)
- **Returns control** to your terminal
- **Services continue running** in the background

## Verifying Installation

### Step 7: Check Running Containers

Verify that CTFd services are running:

<div className="code-header">Terminal</div>

```bash
docker ps
```

**Expected Output:**
You should see running containers for:
- **CTFd application** (main web service)
- **Nginx** (web server)
- **MariaDB** (database)
- **Redis** (caching service)

### Step 8: Check Docker Images

List all downloaded Docker images:

<div className="code-header">Terminal</div>

```bash
docker images
```

**Expected Output:**
Shows all Docker images including:
- CTFd application image
- Database images
- Web server images
- Supporting service images

### Step 9: Test CTFd Access

Check if CTFd is accessible:

<div className="code-header">Terminal</div>

```bash
curl -I "http://localhost/setup"
```

**What this command does:**
- **`curl -I`** - Sends a HEAD request (headers only)
- **`"http://localhost/setup"`** - Tests the CTFd setup endpoint
- **Checks server response** and HTTP status

**Expected Output:**
You should see HTTP headers confirming the server is active:
- **HTTP/1.1 200 OK** - Server is responding
- **Content-Type: text/html** - Proper content type
- **Server headers** - Web server information

## Accessing CTFd

### Step 10: Open CTFd in Browser

Once everything is running, open your web browser and navigate to:

**URL:** `http://localhost`

**What you'll see:**
- CTFd setup page (first time)
- Login page (if already configured)
- Main CTFd interface

### Step 11: Initial Setup

If this is your first time running CTFd:

1. **Create Admin Account** - Set up your administrator credentials
2. **Configure Settings** - Basic platform configuration
3. **Create Challenges** - Add your first CTF challenges
4. **Set Up Teams** - Configure team registration

## Troubleshooting

### Common Issues and Solutions

#### Issue 1: Docker Not Found
**Problem:** `docker: command not found`
**Solution:**
```bash
sudo apt update
sudo apt install docker.io docker-compose -y
sudo systemctl start docker
sudo systemctl enable docker
```

#### Issue 2: Permission Denied
**Problem:** `permission denied while trying to connect to Docker daemon`
**Solution:**
```bash
sudo usermod -aG docker $USER
newgrp docker
```

#### Issue 3: Port Already in Use
**Problem:** Port 80 or 8000 already in use
**Solution:**
```bash
# Check what's using the port
sudo netstat -tulpn | grep :80
# Kill the process or change CTFd port in docker-compose.yml
```

#### Issue 4: Database Connection Issues
**Problem:** Database connection errors
**Solution:**
```bash
# Restart database container
docker-compose restart db
# Check database logs
docker-compose logs db
```

### Useful Docker Commands

<div className="code-header">Terminal</div>

```bash
# Stop CTFd
docker-compose down

# Restart CTFd
docker-compose restart

# View logs
docker-compose logs

# View specific service logs
docker-compose logs web

# Update CTFd
git pull
docker-compose down
docker-compose up --build
```

## SSL Certificate Setup

### Setting Up HTTPS with Let's Encrypt

For production use, it's essential to secure your CTFd platform with SSL certificates. Here's how to set up HTTPS using Let's Encrypt:

### Step 1: Obtain SSL Certificate

Use Certbot to obtain a free SSL certificate from Let's Encrypt:

<div className="code-header">Terminal</div>

```bash
docker run -it --rm --name certbot \
  -v "/etc/letsencrypt:/etc/letsencrypt" \
  -v "/var/lib/letsencrypt:/var/lib/letsencrypt" \
  -p 80:80 -p 443:443 certbot/certbot certonly
```

**What this command does:**
- **`docker run -it --rm`** - Runs Certbot container interactively and removes it after completion
- **`--name certbot`** - Names the container for easy reference
- **`-v "/etc/letsencrypt:/etc/letsencrypt"`** - Mounts Let's Encrypt configuration directory
- **`-v "/var/lib/letsencrypt:/var/lib/letsencrypt"`** - Mounts Let's Encrypt data directory
- **`-p 80:80 -p 443:443`** - Exposes HTTP and HTTPS ports
- **`certbot/certbot certonly`** - Runs Certbot in certificate-only mode

**Interactive Setup:**
1. **Choose authentication method** - Select "standalone" for automatic verification
2. **Enter your domain** - e.g., `your-domain.com`
3. **Verify domain ownership** - Certbot will automatically verify your domain
4. **Certificate generated** - SSL certificate will be created and stored

### Step 2: Copy SSL Certificates

Copy the generated certificates to your CTFd configuration directory:

<div className="code-header">Terminal</div>

```bash
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem ./conf/nginx/fullchain.pem
cp /etc/letsencrypt/live/your-domain.com/privkey.pem ./conf/nginx/privkey.pem
```

**What these commands do:**
- **`cp /etc/letsencrypt/live/your-domain.com/fullchain.pem`** - Copies the full certificate chain
- **`cp /etc/letsencrypt/live/your-domain.com/privkey.pem`** - Copies the private key
- **`./conf/nginx/`** - Destination directory for CTFd SSL configuration

### Step 3: Configure Docker Compose for SSL

Update your `docker-compose.yml` to include SSL certificate volumes:

<div className="code-header">docker-compose.yml</div>

```yaml
services:
  ctfd:
    build: .
    user: root
    restart: always
    ports:
      - "8000:8000"
    environment:
      - UPLOAD_FOLDER=/var/uploads
      - DATABASE_URL=mysql+pymysql://ctfd:ctfd@db/ctfd
      - REDIS_URL=redis://cache:6379
      - WORKERS=1
      - LOG_FOLDER=/var/log/CTFd
      - ACCESS_LOG=-
      - ERROR_LOG=-
      - REVERSE_PROXY=true
    volumes:
      - .data/CTFd/logs:/var/log/CTFd
      - .data/CTFd/uploads:/var/uploads
      - .:/opt/CTFd:ro
    depends_on:
      - db
    networks:
        default:
        internal:

  nginx:
    image: nginx:stable
    restart: always
    volumes:
      - ./conf/nginx/http.conf:/etc/nginx/nginx.conf
      - ./conf/nginx/fullchain.pem:/certificates/fullchain.pem:ro
      - ./conf/nginx/privkey.pem:/certificates/privkey.pem:ro
    ports:
      - 80:80
      - 443:443
    depends_on:
      - ctfd

  db:
    image: mariadb:10.11
    restart: always
    environment:
      - MARIADB_ROOT_PASSWORD=ctfd
      - MARIADB_USER=ctfd
      - MARIADB_PASSWORD=ctfd
      - MARIADB_DATABASE=ctfd
      - MARIADB_AUTO_UPGRADE=1
    volumes:
      - .data/mysql:/var/lib/mysql
    networks:
        internal:
    # This command is required to set important mariadb defaults
    command: [mysqld, --character-set-server=utf8mb4, --collation-server=utf8mb4_unicode_ci, --wait_timeout=28800, --log-warnings=0]

  cache:
    image: redis:4
    restart: always
    volumes:
    - .data/redis:/data
    networks:
        internal:

networks:
    default:
    internal:
        internal: true

```

**Key SSL Configuration:**
- **`./conf/nginx/fullchain.pem:/certificates/fullchain.pem:ro`** - Mounts full certificate chain
- **`./conf/nginx/privkey.pem:/certificates/privkey.pem:ro`** - Mounts private key
- **`:ro`** - Read-only mount for security

### Step 4: Configure Nginx for SSL

Create or update your Nginx configuration to handle SSL:

<div className="code-header">conf/nginx/nginx.conf</div>

```nginx
worker_processes 4;

events {

  worker_connections 1024;
}

http {

  # Configuration containing list of application servers
  upstream app_servers {

    server ctfd:8000;
  }

  # HTTP server - redirect to HTTPS
  server {
    listen 80;
    server_name <Your Domain>;
    return 301 https://$server_name$request_uri;
  }

  # HTTPS server
  server {
    listen 443 ssl http2;
    server_name <Your Domain>;

    # SSL Configuration
    ssl_certificate /certificates/fullchain.pem;
    ssl_certificate_key /certificates/privkey.pem;
    
    # SSL Security Settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-RSA-AES128-SHA256:ECDHE-RSA-AES256-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    gzip on;
    client_max_body_size 4G;

    # Handle Server Sent Events for Notifications
    location /events {
      proxy_pass http://app_servers;
      proxy_set_header Connection '';
      proxy_http_version 1.1;
      chunked_transfer_encoding off;
      proxy_buffering off;
      proxy_cache off;
      proxy_redirect off;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Host $server_name;
      proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Proxy connections to the application servers
    location / {
      proxy_pass http://app_servers;
      proxy_redirect off;
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Host $server_name;
      proxy_set_header X-Forwarded-Proto $scheme;
    }
  }
}

```

### Step 5: Restart CTFd with SSL

Restart your CTFd services to apply SSL configuration:

<div className="code-header">Terminal</div>

```bash
docker-compose down
docker-compose up -d
```

### Step 6: Verify SSL Configuration

Test your SSL setup:

<div className="code-header">Terminal</div>

```bash
# Test SSL certificate
curl -I "https://your-domain.com"

# Check SSL certificate details
openssl s_client -connect your-domain.com:443 -servername your-domain.com
```

**Expected Results:**
- **HTTPS redirect** - HTTP requests automatically redirect to HTTPS
- **Valid SSL certificate** - Browser shows secure connection
- **Security headers** - Proper security headers in response

### SSL Certificate Renewal

Let's Encrypt certificates expire every 90 days. Set up automatic renewal:

<div className="code-header">Terminal</div>

```bash
# Create renewal script
cat > /etc/cron.d/certbot-renew << EOF
0 12 * * * root docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /var/lib/letsencrypt:/var/lib/letsencrypt certbot/certbot renew --quiet && docker-compose restart nginx
EOF

# Test renewal
docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /var/lib/letsencrypt:/var/lib/letsencrypt certbot/certbot renew --dry-run
```

## Next Steps

### Customizing Your CTF Platform

1. **Theme Customization**
   - Modify CSS and templates
   - Add custom branding
   - Configure color schemes

2. **Challenge Creation**
   - Create web challenges
   - Set up reverse engineering tasks
   - Configure cryptography challenges
   - Add forensics exercises

3. **Advanced Configuration**
   - Set up SSL/HTTPS
   - Configure email notifications
   - Integrate with external services
   - Set up monitoring and logging

### Security Considerations

1. **Network Security**
   - Use reverse proxy (Nginx)
   - Configure firewall rules
   - Enable SSL/TLS encryption

2. **Application Security**
   - Regular updates
   - Secure database configuration
   - Access control and authentication

3. **Challenge Security**
   - Isolate challenge environments
   - Monitor for cheating
   - Secure flag storage

## Conclusion

Congratulations! You've successfully set up CTFd with Docker. You now have a fully functional Capture The Flag platform ready for hosting cybersecurity competitions and educational challenges.

### Key Takeaways

- **Docker simplifies deployment** - Easy containerized setup
- **CTFd is highly customizable** - Adapt to your needs
- **Community support** - Active development and documentation
- **Scalable platform** - From small events to large competitions

### Resources

- **CTFd Documentation:** [https://docs.ctfd.io/](https://docs.ctfd.io/)
- **CTFd GitHub:** [https://github.com/CTFd/CTFd](https://github.com/CTFd/CTFd)
- **Docker Documentation:** [https://docs.docker.com/](https://docs.docker.com/)

### What's Next?

1. **Create your first challenge** - Start with simple web challenges
2. **Invite participants** - Set up team registration
3. **Monitor the competition** - Use admin tools to track progress
4. **Gather feedback** - Improve your platform based on user experience

Happy CTF hosting! 🚩🔒

---

*Have questions about CTFd setup or need help with advanced configurations? Feel free to reach out and let's discuss!*
