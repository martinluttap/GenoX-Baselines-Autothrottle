set -ex


install_containerd() {
   wget https://github.com/containerd/containerd/releases/download/v1.6.14/containerd-1.6.14-linux-amd64.tar.gz
   tar Cxzvf /usr/local containerd-1.6.14-linux-amd64.tar.gz
   wget https://github.com/opencontainers/runc/releases/download/v1.1.3/runc.amd64
   install -m 755 runc.amd64 /usr/local/sbin/runc
  
   mkdir -p /etc/containerd
   containerd config default | tee /etc/containerd/config.toml
   sed -i 's/SystemdCgroup \= false/SystemdCgroup \= true/g' /etc/containerd/config.toml
   curl -L https://raw.githubusercontent.com/containerd/containerd/main/containerd.service -o /etc/systemd/system/containerd.service
  
   systemctl daemon-reload
   systemctl enable --now containerd
   systemctl status containerd
   systemctl restart containerd
}


install_docker() {
   apt-get update
   apt-get install ca-certificates curl
   install -m 0755 -d /etc/apt/keyrings
   curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
   chmod a+r /etc/apt/keyrings/docker.asc


   # Add the repository to Apt sources:
   echo \
   "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
   $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
   tee /etc/apt/sources.list.d/docker.list > /dev/null
   apt-get update


   apt-get install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin


   groupadd docker
   usermod -aG docker $USER
   newgrp docker


   echo '{"exec-opts":["native.cgroupdriver=systemd"],"log-driver":"json-file","log-opts":{"max-size":"100m"},"storage-driver":"overlay2"}' >/etc/docker/daemon.json
   systemctl restart docker
   docker run hello-world
}


install_k8s() {
   apt-get update
   apt-get install -y apt-transport-https ca-certificates curl gpg
   mkdir -p /etc/apt/keyrings/
   curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.28/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
   echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.28/deb/ /' | tee /etc/apt/sources.list.d/kubernetes.list
   apt-get update
   apt-get install -y socat
   apt-get install -y kubelet kubeadm kubectl
}




# systemctl stop firewalld.service


# Why do we need to do this?? Taken from here:
# https://github.com/kubernetes/kubeadm/issues/1062
# echo "net.bridge.bridge-nf-call-iptables=1" | tee -a /etc/sysctl.conf
# modprobe br_netfilter
# echo '1' > /proc/sys/net/ipv4/ip_forward


# install_containerd
# install_k8s


if [ "$1" = master ]; then
   # initialize Kubernetes cluster
   kubeadm init --pod-network-cidr=10.244.0.0/16


   # setup Kubernetes credentials
   mkdir -p .kube
   cp /etc/kubernetes/admin.conf .kube/config


   # setup Kubernetes networking
   kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml


   # save join command and credentials
   kubeadm token create --print-join-command >join-command
   cp .kube/config kube-config


   # install Python dependencies
   # apt-get install -y python3-venv
   # python3 -m venv venv
   # venv/bin/pip install -r requirements.txt


   # make Locust available globally
   # ln -s /root/venv/bin/locust /usr/local/bin/locust


elif [ "$1" = worker ]; then
   # join Kubernetes cluster
   bash join-command


   # setup Kubernetes credentials
   mkdir -p .kube
   cp kube-config .kube/config


   # run worker daemon in background
   tmux new-session -d './worker-daemon.py'
fi
