FROM ubuntu:14.04

RUN apt-get update && apt-get install -y \
    python-pip \
    libpython2.7 \
    libboost1.55-all-dev \
    python-pygments \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Manually download and install Flask to bypass SSL issues
RUN curl -k -L https://files.pythonhosted.org/packages/2e/80/3726a729de758513fd3dbc64e93098eb009c49305a97c6751de55b20b694/Flask-1.1.4-py2.py3-none-any.whl -o Flask-1.1.4-py2.py3-none-any.whl
RUN curl -k -L https://files.pythonhosted.org/packages/8a/bb/1a5377c2db9af4f838a1905e89d9b9acb3d3cb3c68e57915a675f07a49a4/Werkzeug-1.0.1-py2.py3-none-any.whl -o Werkzeug-1.0.1-py2.py3-none-any.whl
RUN curl -k -L https://files.pythonhosted.org/packages/4c/4d/88bc941f61e7dc7674189637091e41954bf658850ac7b0e93322c23a163/Jinja2-2.11.3-py2.py3-none-any.whl -o Jinja2-2.11.3-py2.py3-none-any.whl
RUN curl -k -L https://files.pythonhosted.org/packages/8e/76/66066b246bc2d806fbf236cb56ccc7e528649bf5a7c12e7257777636b8ba/MarkupSafe-1.1.1-cp27-cp27m-manylinux1_x86_64.whl -o MarkupSafe-1.1.1-cp27-cp27m-manylinux1_x86_64.whl
RUN curl -k -L https://files.pythonhosted.org/packages/76/3a/51e501b4da3addf9a1c028fcd8b4fd148b0e253f31d5eec92f2c26bb88ec/itsdangerous-1.1.0-py2.py3-none-any.whl -o itsdangerous-1.1.0-py2.py3-none-any.whl
RUN curl -k -L https://files.pythonhosted.org/packages/d2/3d/fa76db83bf75c4f8d335c1fa1b6c422872e2f000c1a58a9c5a26ac90c508/click-7.1.2-py2.py3-none-any.whl -o click-7.1.2-py2.py3-none-any.whl

# Install the downloaded packages
RUN pip install --no-index --find-links . Flask-1.1.4-py2.py3-none-any.whl Werkzeug-1.0.1-py2.py3-none-any.whl Jinja2-2.11.3-py2.py3-none-any.whl MarkupSafe-1.1.1-cp27-cp27m-manylinux1_x86_64.whl itsdangerous-1.1.0-py2.py3-none-any.whl click-7.1.2-py2.py3-none-any.whl

RUN mkdir /nao-bridge
WORKDIR /nao-bridge
COPY src/ /nao-bridge