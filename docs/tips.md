#### 压缩项目



```
cd /home/wmd/projects
sudo tar --exclude='VideoClaw/data' -czf VideoClaw.tar.gz VideoClaw
```


```
cd /home/wmd/projects
sudo tar \
  --exclude='VideoClaw/data' \
  --exclude='*/.git' \
  --exclude='*/.qoder' \
  --exclude='*/.agents' \
  --exclude='*/.codebuddy' \
  -czf VideoClaw.tar.gz VideoClaw
```
