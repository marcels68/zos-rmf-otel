//ZSMFLIVE JOB (ACCT),'SMF REALTIME OTEL',CLASS=A,MSGCLASS=X,
//             NOTIFY=&SYSUID,REGION=0M,LINES=500000
//*--------------------------------------------------------------------
//* zsmf-otel als Dauerlaeufer: SMF Real-Time Interface -> OTLP.
//* JZOS Batch Launcher; JVMLDMnn passend zur Java-Version waehlen
//* (z. B. JVMLDM17 fuer Java 17, JVMLDM21 fuer Java 21).
//* Optionen stehen in ZSMF_ARGS (STDENV), weil PARM max. 100 Zeichen.
//* Benutzer braucht READ auf FACILITY IFA.IFASMF.OTEL (siehe README).
//*--------------------------------------------------------------------
//JAVA     EXEC PGM=JVMLDM17,REGION=0M,
//             PARM='/ zsmf.Main'
//STEPLIB  DD DSN=JAVA.SJVMLOAD,DISP=SHR     <- Ladebibl. JZOS anpassen
//SYSPRINT DD SYSOUT=*
//SYSOUT   DD SYSOUT=*
//STDOUT   DD SYSOUT=*
//STDERR   DD SYSOUT=*
//STDENV   DD *
export JAVA_HOME=/usr/lpp/java/J17.0_64
export APP=/u/zuser/zsmf
export CLASSPATH=$APP/zsmf-otel.jar
export LIBPATH=$APP:$JAVA_HOME/lib/s390x:$JAVA_HOME/lib/s390x/j9vm
export IBM_JAVA_OPTIONS="-Xms64m -Xmx512m -Djava.library.path=$APP"
export ZSMF_ARGS="--source inmem:IFASMF.OTEL
 --otlp http://collector.example.com:4318 --tz Europe/Berlin
 --status-port 9464 --checkpoint $APP/zsmf.checkpoint"
/*
