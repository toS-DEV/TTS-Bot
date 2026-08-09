function sendBirthdayOrMemorialMessages() {
  const lock = LockService.getScriptLock();
  try {
    if (!lock.tryLock(5000)) {
      Logger.log('別プロセスが実行中のため、今回の実行をスキップします');
      return;
    }
    Logger.log('スクリプトの実行を開始します');

    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('days');
    const data = sheet.getDataRange().getValues();
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()); 
    const todayYear = today.getFullYear();
    const todayMonth = today.getMonth() + 1;
    const todayDay = today.getDate();

    Logger.log(`今日の日付: ${todayYear}-${todayMonth}-${todayDay}`);

    let messageA = '';  // (A)に追加するメッセージ
    let messageB = '';  // (B)に追加するメッセージ
    let messageC = '';  // (C)に追加するメッセージ

    for (let i = 1; i < data.length; i++) {
      const [id, year, month, day, text, memo, mode] = data[i];
      Logger.log(`現在処理中の行: ID=${id}, year=${year}, month=${month}, day=${day}, text=${text}, mode=${mode}`);

      // (A)の処理: 月と日が一致しているか確認
      if (Number(month) === todayMonth && Number(day) === todayDay) {
        Logger.log(`(A)処理 - 月と日が一致しました: ${text} (${month}/${day})`);

        if (mode === 'birthday') {
          if (year !== '' && year != null) {
            const age = todayYear - year;
            messageA += `## ${text} さん${age}歳の誕生日です！\n`;
          } else {
            messageA += `## ${text} さんの誕生日です！\n`;
          }
        } else if (mode === 'memorial') {
          if (year !== '' && year != null) {
            const yearsSince = todayYear - year;
            messageA += `${text} からちょうど${yearsSince}年が経ちました！\n`;
          } else {
            messageA += `${text} です！\n`;
          }
        }
      } else {
        Logger.log(`(A)処理 - 月と日が一致しませんでした: ${text} (${month}/${day})`);
      }

    // (B)の処理: year がある場合にキリ番、ゾロ目、連続する数字を判定
    if (year !== '' && year != null) {
      const eventDate = new Date(year, month - 1, day);
        eventDate.setHours(0, 0, 0, 0);
        
        const diffTime = today.getTime() - eventDate.getTime();
        const daysSince = Math.floor(diffTime / (1000 * 60 * 60 * 24));

        if (daysSince >= 10) {
          if (isSpecialNumber(daysSince)) {
            if (mode === 'birthday') {
              messageB += `${text} さんが生まれてから${daysSince}日です！\n`;
            } else if (mode === 'memorial') {
              messageB += `${text} から${daysSince}日です！\n`;
            }
          }
        }
        if (daysSince < 0) {
        Logger.log(`記述された日付が今日よりも未来側です！`)
      }
      }
    }
    // --- カウントダウン機能 ---
    const countdownSheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('countdown');
    const countdownData = countdownSheet.getDataRange().getValues();
    let countdownMap = new Map();
    let rowsToDelete = [];

    for (let j = 1; j < countdownData.length; j++) {
      const [cYear, cMonth, cDay, title, description, tag] = countdownData[j];
      if (!cYear || !cMonth || !cDay) continue;

      const eventDate = new Date(cYear, cMonth - 1, cDay);
      eventDate.setHours(0, 0, 0, 0);
      
      const diffTime = eventDate.getTime() - today.getTime();
      const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));

      if (diffDays < 0) {
        rowsToDelete.push(j + 1);
        continue;
      }

      if (diffDays <= 14) {
        const eventInfo = { diffDays, title, description, rowIndex: j + 1 };
        if (!tag) {
          countdownMap.set(`no-tag-${j}`, eventInfo);
        } else {
          if (!countdownMap.has(tag) || diffDays < countdownMap.get(tag).diffDays) {
            countdownMap.set(tag, eventInfo);
          }
        }
      }
    }

    // 2. メッセージの組み立て
    countdownMap.forEach((info) => {
      if (info.diffDays === 0) {
        messageC += `## 当日：${info.title} \n`;
        if (info.description) {
          const formattedDescription = String(info.description)
            .split('\n').map(line => line.trim() ? `> ${line}` : '').join('\n');
          messageC += `${formattedDescription}\n\n`;
        }
      } else {
        messageC += `-# あと**${info.diffDays}日**：${info.title} \n`;
      }
    });

    // 3. 終わったイベントをシートから削除（下の行から）
    rowsToDelete.sort((a, b) => b - a).forEach(rowIndex => {
      countdownSheet.deleteRow(rowIndex);
    });

    // メッセージを送信
    if (messageA || messageB || messageC) {
      let finalMessage = '今日は\n';
      if (messageA) finalMessage += messageA;
      if (messageB) finalMessage += messageB;
      if (messageC) {
        if (messageA || messageB) finalMessage += '\n--- カウントダウン ---\n';
        finalMessage += messageC;
      }
      finalMessage += '\n\n誕生日、記念日の追加、編集、削除はこちらから：[ネネロボ(記念日)のフォーム](https://docs.google.com/forms/d/e... )\n';
      
      
      dispatchDiscordMessage(finalMessage);
      
    } else {
      Logger.log('該当するイベントはありませんでした');
    }

  } catch (e) {
    Logger.log(`[Error] 実行中に例外が発生: ${e.stack}`);
  } finally {
    // 最後に必ず鍵を開ける
    lock.releaseLock();
  }
}

function dispatchDiscordMessage(message) {
  const props = PropertiesService.getScriptProperties();
  
  // 1. まず失敗したときのためにメッセージを保存しておく
  props.setProperty('RETRY_MESSAGE', message);
  
  // 2. 送信を試みる
  const success = postToDiscord(message);
  
  if (success) {
    // 3. 成功したら保存したメッセージを消す
    props.deleteProperty('RETRY_MESSAGE');
    Logger.log('配信に成功したため、バックアップを削除しました。');
  } else {
    // 4. 失敗した場合は postToDiscord 内で scheduleRetry が呼ばれているはず
    Logger.log('配信失敗。メッセージはPropertiesServiceに保持されています。');
  }
}

function retryDiscordPost() {
  const lock = LockService.getScriptLock();
  try {
    if (!lock.tryLock(5000)) {
      Logger.log('別プロセス（メイン等）が実行中のため、リトライをスキップします');
      return;
    }
    const props = PropertiesService.getScriptProperties();
    const message = props.getProperty('RETRY_MESSAGE');

    if (!message) {
      Logger.log('リトライ対象のメッセージがありません。処理を終了します。');
      deleteCurrentTrigger('retryDiscordPost'); // ゴミ掃除
      return;
    }

    Logger.log('リトライ送信を開始します...');
    const success = postToDiscord(message);

    if (success) {
      props.deleteProperty('RETRY_MESSAGE');
      deleteCurrentTrigger('retryDiscordPost');
      Logger.log('リトライ成功。トリガーとバックアップを削除しました。');
    } else {
      Logger.log('リトライも失敗。再度トリガーが設定されるのを待ちます。');
    }
    
  } catch (e) {
  Logger.log(`[Error] リトライ中に例外が発生: ${e.message}`);
  } finally {
    // 最後に必ず鍵を開ける
    lock.releaseLock();
  }
}

// キリ番、ゾロ目、連続数字の判定
function isSpecialNumber(number) {
  const strNum = String(number);

  // 1桁目以降が全て0の数字 (キリ番)
  const allZeros = /^([1-9])0+$/.test(strNum);

  // ゾロ目 (全ての桁が同じ数字)
  const sameDigits = /^([0-9])\1+$/.test(strNum);

  // 数字が連続しているか（昇順・降順）
  const increasing = '0123456789'.includes(strNum);
  const decreasing = '9876543210'.includes(strNum);

  const result = allZeros || sameDigits || increasing || decreasing;
  Logger.log(`isSpecialNumber(${number}): ${result}`);
  return result;
}

// Discord Webhookにメッセージを送信する関数
function postToDiscord(message, url) {
  const targetUrl = url ||'https://discord.com/api/webhooks/...'; // Webhook URLをここに設定
  //const targetUrl = url || 'https://discord.com/api/webhooks/...'  //開発用
  // Discord Webhookにメッセージを送信する関数（1015回避・再予約機能付き）
  const options = {
    method: 'POST',
    contentType: 'application/json',
    payload: JSON.stringify({ content: message }),
    muteHttpExceptions: true
  };

  for (let i = 0; i < 3; i++) {
    try {
      const response = UrlFetchApp.fetch(targetUrl, options);
      const responseCode = response.getResponseCode();

      if (responseCode === 204 || responseCode === 200) return true;

      if (responseCode === 429) {
        const content = response.getContentText();
        if (content.includes("1015")) {
          scheduleRetry(); // メッセージはPropertiesにあるので引数なしでOKに
          return false;
        }
        // retry_after 待機ロジックは維持
        Utilities.sleep(5000); 
        continue;
      }
    } catch (e) {
      Logger.log(`Fetchエラー(リトライ ${i+1}回目): ${e.message}`);
      Utilities.sleep(2000);
    }
  }
  Logger.log('429が継続したため後で再試行します');
  scheduleRetry();
  return false;
}

// 1015エラー時に2分後のトリガーを作る関数
function scheduleRetry() {
  const functionName = 'retryDiscordPost'; // 【重要】メインではなくリトライ専用関数を呼ぶ
  
  // 二重予約防止
  deleteCurrentTrigger(functionName);

  const retryDate = new Date();
  retryDate.setMinutes(retryDate.getMinutes() + 5);

  ScriptApp.newTrigger(functionName)
    .timeBased()
    .at(retryDate)
    .create();

  Logger.log(`リトライ用トリガーを5分後に設定しました。`);
}

// 朝8時にトリガーを作成
function setDailyTrigger() {
  const functionName = 'sendBirthdayOrMemorialMessages';
    const adminWebhookUrl = 'https://discord.com/api/webhooks/...';
  try {
    // 既存の同名トリガーを掃除
    const triggers = ScriptApp.getProjectTriggers();
    for (const trigger of triggers) {
      if (trigger.getHandlerFunction() === functionName) {
        ScriptApp.deleteTrigger(trigger);
      }
    }

    const today = new Date();
    today.setHours(8, 0, 0, 0); 
    
    if (today <= new Date()) {
      today.setDate(today.getDate() + 1);
    }

    // 8時の実行用トリガーを作成
    ScriptApp.newTrigger(functionName)
      .timeBased()
      .at(today)
      .create();

    // 正常終了時はログにだけ記録（Discordへは送らない）
    Logger.log(`[管理] ${Utilities.formatDate(today, 'JST', 'yyyy/MM/dd HH:mm')} のトリガーを予約したよ。`);

  } catch (e) {
    // もしトリガー設定自体が失敗した場合は、管理者に通知する
    const errorMessage = `【重大なエラー】トリガーの予約に失敗したよ！\nエラー内容: ${e.message}`;
    Logger.log(errorMessage);
    postToDiscord(errorMessage, adminWebhookUrl);
  }
}

function deleteCurrentTrigger(functionName) {
  const triggers = ScriptApp.getProjectTriggers();
  let count = 0;
  for (const trigger of triggers) {
    if (trigger.getHandlerFunction() === functionName) {
      ScriptApp.deleteTrigger(trigger);
      count++;
    }
  }
  Logger.log(`${count} 個のトリガーを掃除したよ。`);
}# TTS-Bot
